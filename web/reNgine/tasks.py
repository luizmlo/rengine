import csv
import json
import os
import pprint
import subprocess
import time
import validators
import whatportis
import xmltodict
import yaml
import tldextract

from datetime import datetime
from urllib.parse import urlparse
from api.serializers import SubdomainSerializer
from celery import chain, chord, group
from celery.result import allow_join_result
from celery.utils.log import get_task_logger
from degoogle import degoogle
from django.db.models import Count
from dotted_dict import DottedDict
from django.utils import timezone
from pycvesearch import CVESearch
from metafinder.extractor import extract_metadata_from_google_search
from emailfinder.extractor import (get_emails_from_baidu, get_emails_from_bing, get_emails_from_google)

from reNgine.celery import app
from reNgine.celery_custom_task import RengineTask
from reNgine.common_func import *
from reNgine.definitions import *
from reNgine.settings import *
from reNgine.gpt import *
from scanEngine.models import (EngineType, InstalledExternalTool, Notification, Proxy)
from startScan.models import *
from startScan.models import EndPoint, Subdomain, Vulnerability
from targetApp.models import Domain

"""
Celery tasks.
"""

logger = get_task_logger(__name__)


#----------------------#
# Scan / Subscan tasks #
#----------------------#


@app.task
def initiate_scan(
		scan_history_id,
		domain_id,
		engine_id=None,
		scan_type=LIVE_SCAN,
		results_dir=RENGINE_RESULTS,
		imported_subdomains=[],
		out_of_scope_subdomains=[],
		url_filter=''):
	"""Initiate a new scan.

	Args:
		scan_history_id (int): ScanHistory id.
		domain_id (int): Domain id.
		engine_id (int): Engine ID.
		scan_type (int): Scan type (periodic, live).
		results_dir (str): Results directory.
		imported_subdomains (list): Imported subdomains.
		out_of_scope_subdomains (list): Out-of-scope subdomains.
		url_filter (str): URL path. Default: ''
	"""

	# Get scan history
	scan = ScanHistory.objects.get(pk=scan_history_id)

	# Get scan engine
	engine_id = engine_id or scan.scan_type.id # scan history engine_id
	engine = EngineType.objects.get(pk=engine_id)

	# Get YAML config
	config = yaml.safe_load(engine.yaml_configuration)
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)
	gf_patterns = config.get(GF_PATTERNS, [])

	# Get domain and set last_scan_date
	domain = Domain.objects.get(pk=domain_id)
	domain.last_scan_date = timezone.now()
	domain.save()

	# Get path filter
	url_filter = url_filter.rstrip('/')

	# Get or create ScanHistory() object
	if scan_type == LIVE_SCAN: # immediate
		scan = ScanHistory.objects.get(pk=scan_history_id)
		scan.scan_status = RUNNING_TASK
	elif scan_type == SCHEDULED_SCAN: # scheduled
		scan = ScanHistory()
		scan.scan_status = INITIATED_TASK
	scan.scan_type = engine
	scan.celery_ids = [initiate_scan.request.id]
	scan.domain = domain
	scan.start_scan_date = timezone.now()
	scan.tasks = engine.tasks
	scan.results_dir = f'{results_dir}/{domain.name}_{scan.id}'
	add_gf_patterns = gf_patterns and 'fetch_url' in engine.tasks
	if add_gf_patterns:
		scan.used_gf_patterns = ','.join(gf_patterns)
	scan.save()

	# Create scan results dir
	os.makedirs(scan.results_dir)

	# Build task context
	ctx = {
		'scan_history_id': scan_history_id,
		'engine_id': engine_id,
		'domain_id': domain.id,
		'results_dir': scan.results_dir,
		'url_filter': url_filter,
		'yaml_configuration': config,
		'out_of_scope_subdomains': out_of_scope_subdomains
	}
	ctx_str = json.dumps(ctx, indent=2)

	# Send start notif
	logger.warning(f'Starting scan {scan_history_id} with context:\n{ctx_str}')
	send_scan_notif.delay(
		scan_history_id,
		subscan_id=None,
		engine_id=engine_id,
		status=CELERY_TASK_STATUS_MAP[scan.scan_status])

	# Save imported subdomains in DB
	save_imported_subdomains(imported_subdomains, ctx=ctx)

	# Create initial subdomain in DB: make a copy of domain as a subdomain so
	# that other tasks using subdomains can use it.
	subdomain_name = domain.name
	subdomain, _ = save_subdomain(subdomain_name, ctx=ctx)

	# If enable_http_crawl is set, create an initial root HTTP endpoint so that
	# HTTP crawling can start somewhere
	http_url = f'{domain.name}{url_filter}' if url_filter else domain.name
	endpoint, _ = save_endpoint(
		http_url,
		ctx=ctx,
		crawl=enable_http_crawl,
		is_default=True,
		subdomain=subdomain
	)
	if endpoint and endpoint.is_alive:
		# TODO: add `root_endpoint` property to subdomain and simply do
		# subdomain.root_endpoint = endpoint instead
		logger.warning(f'Found subdomain root HTTP URL {endpoint.http_url}')
		subdomain.http_url = endpoint.http_url
		subdomain.http_status = endpoint.http_status
		subdomain.response_time = endpoint.response_time
		subdomain.page_title = endpoint.page_title
		subdomain.content_type = endpoint.content_type
		subdomain.content_length = endpoint.content_length
		for tech in endpoint.technologies.all():
			subdomain.technologies.add(tech)
		subdomain.save()


	# Build Celery tasks, crafted according to the dependency graph below:
	# subdomain_discovery --> port_scan --> fetch_url --> dir_file_fuzz
	# osint								             	  vulnerability_scan
	# osint								             	  dalfox xss scan
	#						 	   		         	  	  screenshot
	#													  waf_detection
	workflow = chain(
		group(
			subdomain_discovery.si(ctx=ctx, description='Subdomain discovery'),
			osint.si(ctx=ctx, description='OS Intelligence')
		),
		port_scan.si(ctx=ctx, description='Port scan'),
		fetch_url.si(ctx=ctx, description='Fetch URL'),
		group(
			dir_file_fuzz.si(ctx=ctx, description='Directories & files fuzz'),
			vulnerability_scan.si(ctx=ctx, description='Vulnerability scan'),
			dalfox_xss_scan.si(ctx=ctx, description='Dalfox XSS scan'),
			crlfuzz.si(ctx=ctx, description='CRLF Fuzz'),
			screenshot.si(ctx=ctx, description='Screenshot'),
			waf_detection.si(ctx=ctx, description='WAF detection')
		)
	)

	# Build callback
	callback = report.si(ctx=ctx).set(link_error=[report.si(ctx=ctx)])

	# Run Celery chord
	logger.info(f'Running Celery workflow with {len(workflow.tasks) + 1} tasks')
	task = chain(workflow, callback).on_error(callback).delay()
	scan.celery_ids.append(task.id)
	scan.save()

	return {
		'success': True,
		'task_id': task.id
	}


@app.task
def initiate_subscan(
		scan_history_id,
		subdomain_id,
		engine_id=None,
		scan_type=None,
		results_dir=RENGINE_RESULTS,
		url_filter=''):
	"""Initiate a new subscan.

	Args:
		scan_history_id (int): ScanHistory id.
		subdomain_id (int): Subdomain id.
		engine_id (int): Engine ID.
		scan_type (int): Scan type (periodic, live).
		results_dir (str): Results directory.
		url_filter (str): URL path. Default: ''
	"""

	# Get Subdomain and ScanHistory
	subdomain = Subdomain.objects.get(pk=subdomain_id)
	scan = ScanHistory.objects.get(pk=subdomain.scan_history.id)

	# Get EngineType
	engine_id = engine_id or scan.scan_type.id
	engine = EngineType.objects.get(pk=engine_id)

	# Get YAML config
	config = yaml.safe_load(engine.yaml_configuration)
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)

	# Create scan activity of SubScan Model
	subscan = SubScan(
		start_scan_date=timezone.now(),
		celery_ids=[initiate_subscan.request.id],
		scan_history=scan,
		subdomain=subdomain,
		type=scan_type,
		status=RUNNING_TASK,
		engine=engine)
	subscan.save()

	# Get YAML configuration
	config = yaml.safe_load(engine.yaml_configuration)

	# Create results directory
	results_dir = f'{scan.results_dir}/subscans/{subscan.id}'
	os.makedirs(results_dir, exist_ok=True)

	# Run task
	method = globals().get(scan_type)
	if not method:
		logger.warning(f'Task {scan_type} is not supported by reNgine. Skipping')
		return
	scan.tasks.append(scan_type)
	scan.save()

	# Send start notif
	send_scan_notif.delay(
		scan.id,
		subscan_id=subscan.id,
		engine_id=engine_id,
		status='RUNNING')

	# Build context
	ctx = {
		'scan_history_id': scan.id,
		'subscan_id': subscan.id,
		'engine_id': engine_id,
		'subdomain_id': subdomain.id,
		'yaml_configuration': config,
		'results_dir': results_dir,
		'url_filter': url_filter
	}

	# Create initial endpoints in DB: find domain HTTP endpoint so that HTTP
	# crawling can start somewhere
	base_url = f'{subdomain.name}{url_filter}' if url_filter else subdomain.name
	endpoint, _ = save_endpoint(
		base_url,
		crawl=enable_http_crawl,
		ctx=ctx,
		subdomain=subdomain)
	if endpoint and endpoint.is_alive:
		# TODO: add `root_endpoint` property to subdomain and simply do
		# subdomain.root_endpoint = endpoint instead
		logger.warning(f'Found subdomain root HTTP URL {endpoint.http_url}')
		subdomain.http_url = endpoint.http_url
		subdomain.http_status = endpoint.http_status
		subdomain.response_time = endpoint.response_time
		subdomain.page_title = endpoint.page_title
		subdomain.content_type = endpoint.content_type
		subdomain.content_length = endpoint.content_length
		for tech in endpoint.technologies.all():
			subdomain.technologies.add(tech)
		subdomain.save()

	# Build header + callback
	workflow = method.si(ctx=ctx)
	callback = report.si(ctx=ctx).set(link_error=[report.si(ctx=ctx)])

	# Run Celery tasks
	task = chain(workflow, callback).on_error(callback).delay()
	subscan.celery_ids.append(task.id)
	subscan.save()

	return {
		'success': True,
		'task_id': task.id
	}


@app.task
def report(ctx={}, description=None):
	"""Report task running after all other tasks.
	Mark ScanHistory or SubScan object as completed and update with final
	status, log run details and send notification.

	Args:
		description (str, optional): Task description shown in UI.
	"""
	# Get objects
	subscan_id = ctx.get('subscan_id')
	scan_id = ctx.get('scan_history_id')
	engine_id = ctx.get('engine_id')
	scan = ScanHistory.objects.filter(pk=scan_id).first()
	subscan = SubScan.objects.filter(pk=subscan_id).first()

	# Get failed tasks
	tasks = ScanActivity.objects.filter(scan_of=scan).all()
	if subscan:
		tasks = tasks.filter(celery_id__in=subscan.celery_ids)
	failed_tasks = tasks.filter(status=FAILED_TASK)

	# Get task status
	failed_count = failed_tasks.count()
	status = SUCCESS_TASK if failed_count == 0 else FAILED_TASK
	status_h = 'SUCCESS' if failed_count == 0 else 'FAILED'

	# Update scan / subscan status
	if subscan:
		subscan.stop_scan_date = timezone.now()
		subscan.status = status
		subscan.save()
	else:
		scan.scan_status = status
	scan.stop_scan_date = timezone.now()
	scan.save()

	# Send scan status notif
	send_scan_notif.delay(
		scan_history_id=scan_id,
		subscan_id=subscan_id,
		engine_id=engine_id,
		status=status_h)


#------------------------- #
# Tracked reNgine tasks    #
#--------------------------#

@app.task(base=RengineTask, bind=True)
def subdomain_discovery(
		self,
		host=None,
		ctx=None,
		description=None):
	"""Uses a set of tools (see SUBDOMAIN_SCAN_DEFAULT_TOOLS) to scan all
	subdomains associated with a domain.

	Args:
		host (str): Hostname to scan.

	Returns:
		subdomains (list): List of subdomain names.
	"""
	if not host:
		host = self.subdomain.name if self.subdomain else self.domain.name

	if self.url_filter:
		logger.warning(f'Ignoring subdomains scan as an URL path filter was passed ({self.url_filter}).')
		return

	# Config
	config = self.yaml_configuration.get(SUBDOMAIN_DISCOVERY) or {}
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL) or self.yaml_configuration.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)
	threads = config.get(THREADS) or self.yaml_configuration.get(THREADS, DEFAULT_THREADS)
	timeout = config.get(TIMEOUT) or self.yaml_configuration.get(TIMEOUT, DEFAULT_HTTP_TIMEOUT)
	tools = config.get(USES_TOOLS, SUBDOMAIN_SCAN_DEFAULT_TOOLS)
	default_subdomain_tools = [tool.name.lower() for tool in InstalledExternalTool.objects.filter(is_default=True).filter(is_subdomain_gathering=True)]
	custom_subdomain_tools = [tool.name.lower() for tool in InstalledExternalTool.objects.filter(is_default=False).filter(is_subdomain_gathering=True)]
	send_subdomain_changes, send_interesting = False, False
	notif = Notification.objects.first()
	if notif:
		send_subdomain_changes = notif.send_subdomain_changes_notif
		send_interesting = notif.send_interesting_notif

	# Gather tools to run for subdomain scan
	if ALL in tools:
		tools = SUBDOMAIN_SCAN_DEFAULT_TOOLS + custom_subdomain_tools
	tools = [t.lower() for t in tools]

	# Make exception for amass since tool name is amass, but command is amass-active/passive
	default_subdomain_tools.append('amass-passive')
	default_subdomain_tools.append('amass-active')

	# Run tools
	for tool in tools:
		cmd = None
		logger.info(f'Scanning subdomains with {tool}')
		proxy = get_random_proxy()

		if tool in default_subdomain_tools:
			if tool == 'amass-passive':
				cmd = f'amass enum -passive -d {host} -o {self.results_dir}/subdomains_amass.txt'
				cmd += ' -config /root/.config/amass.ini' if use_amass_config else ''

			elif tool == 'amass-active':
				use_amass_config = config.get(USE_AMASS_CONFIG, False)
				amass_wordlist_name = config.get(AMASS_WORDLIST, 'deepmagic.com-prefixes-top50000')
				wordlist_path = f'/usr/src/wordlist/{amass_wordlist_name}.txt'
				cmd = f'amass enum -active -d {host} -o {self.results_dir}/subdomains_amass_active.txt'
				cmd += ' -config /root/.config/amass.ini' if use_amass_config else ''
				cmd += f' -brute -w {wordlist_path}'

			elif tool == 'sublist3r':
				cmd = f'python3 /usr/src/github/Sublist3r/sublist3r.py -d {host} -t {threads} -o {self.results_dir}/subdomains_sublister.txt'

			elif tool == 'subfinder':
				cmd = f'subfinder -d {host} -o {self.results_dir}/subdomains_subfinder.txt'
				use_subfinder_config = config.get(USE_SUBFINDER_CONFIG, False)
				cmd += ' -config /root/.config/subfinder/config.yaml' if use_subfinder_config else ''
				cmd += f' -proxy {proxy}' if proxy else ''
				cmd += f' -timeout {timeout}' if timeout else ''
				cmd += f' -t {threads}' if threads else ''
				cmd += f' -silent'

			elif tool == 'oneforall':
				cmd = f'python3 /usr/src/github/OneForAll/oneforall.py --target {host} run'
				cmd_extract = f'cut -d\',\' -f6 /usr/src/github/OneForAll/results/{host}.csv > {self.results_dir}/subdomains_oneforall.txt'
				cmd_rm = f'rm -rf /usr/src/github/OneForAll/results/{host}.csv'
				cmd += f' && {cmd_extract} && {cmd_rm}'

			elif tool == 'ctfr':
				results_file = self.results_dir + '/subdomains_ctfr.txt'
				cmd = f'python3 /usr/src/github/ctfr/ctfr.py -d {host} -o {results_file}'
				cmd_extract = f"cat {results_file} | sed 's/\*.//g' | tail -n +12 | uniq | sort > {results_file}"
				cmd += f' && {cmd_extract}'

			elif tool == 'tlsx':
				results_file = self.results_dir + '/subdomains_tlsx.txt'
				cmd = f'tlsx -san -cn -silent -ro -host {host}'
				cmd += f" | sed -n '/^\([a-zA-Z0-9]\([-a-zA-Z0-9]*[a-zA-Z0-9]\)\?\.\)\+{host}$/p' | uniq | sort"
				cmd += f' > {results_file}'

			elif tool == 'netlas':
				results_file = self.results_dir + '/subdomains_netlas.txt'
				cmd = f'netlas search -d domain -i domain domain:"*.{host}" -f json'
				cmd_extract = f"grep -oE '([a-zA-Z0-9]([-a-zA-Z0-9]*[a-zA-Z0-9])?\.)+{host}'"
				cmd += f' | {cmd_extract} > {results_file}'

		elif tool in custom_subdomain_tools:
			tool_query = InstalledExternalTool.objects.filter(name__icontains=tool.lower())
			if not tool_query.exists():
				logger.error(f'Missing {{TARGET}} and {{OUTPUT}} placeholders in {tool} configuration. Skipping.')
				continue
			custom_tool = tool_query.first()
			cmd = custom_tool.subdomain_gathering_command
			if '{TARGET}' in cmd and '{OUTPUT}' in cmd:
				cmd = cmd.replace('{TARGET}', host)
				cmd = cmd.replace('{OUTPUT}', f'{self.results_dir}/subdomains_{tool}.txt')
				cmd = cmd.replace('{PATH}', custom_tool.github_clone_path) if '{PATH}' in cmd else cmd
		else:
			logger.warning(
				f'Subdomain discovery tool "{tool}" is not supported by reNgine. Skipping.')
			continue

		# Run tool
		try:
			run_command(
				cmd,
				shell=True,
				history_file=self.history_file,
				scan_id=self.scan_id,
				activity_id=self.activity_id)
		except Exception as e:
			logger.error(
				f'Subdomain discovery tool "{tool}" raised an exception')
			logger.exception(e)

	# Gather all the tools' results in one single file. Write subdomains into
	# separate files, and sort all subdomains.
	run_command(
		f'cat {self.results_dir}/subdomains_*.txt > {self.output_path}',
		shell=True,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)
	run_command(
		f'sort -u {self.output_path} -o {self.output_path}',
		shell=True,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)

	with open(self.output_path) as f:
		lines = f.readlines()

	# Parse the output_file file and store Subdomain and EndPoint objects found
	# in db.
	subdomain_count = 0
	subdomains = []
	urls = []
	for line in lines:
		subdomain_name = line.strip()
		valid_url = bool(validators.url(subdomain_name))
		valid_domain = (
			bool(validators.domain(subdomain_name)) or
			bool(validators.ipv4(subdomain_name)) or
			bool(validators.ipv6(subdomain_name)) or
			valid_url
		)
		if not valid_domain:
			logger.error(f'Subdomain {subdomain_name} is not a valid domain, IP or URL. Skipping.')
			continue

		if valid_url:
			subdomain_name = urlparse(subdomain_name).netloc

		if subdomain_name in self.out_of_scope_subdomains:
			logger.error(f'Subdomain {subdomain_name} is out of scope. Skipping.')
			continue

		# Add subdomain
		subdomain, _ = save_subdomain(subdomain_name, ctx=ctx)
		subdomain_count += 1
		subdomains.append(subdomain)
		urls.append(subdomain.name)

	# Bulk crawl subdomains
	if enable_http_crawl:
		ctx['track'] = True
		http_crawl(urls, ctx=ctx, is_ran_from_subdomain_scan=True)

	# Find root subdomain endpoints
	for subdomain in subdomains:
		pass

	# Send notifications
	subdomains_str = '\n'.join([f'• `{subdomain.name}`' for subdomain in subdomains])
	self.notify(fields={
		'Subdomain count': len(subdomains),
		'Subdomains': subdomains_str,
	})
	if send_subdomain_changes and self.scan_id and self.domain_id:
		added = get_new_added_subdomain(self.scan_id, self.domain_id)
		removed = get_removed_subdomain(self.scan_id, self.domain_id)

		if added:
			subdomains_str = '\n'.join([f'• `{subdomain}`' for subdomain in added])
			self.notify(fields={'Added subdomains': subdomains_str})

		if removed:
			subdomains_str = '\n'.join([f'• `{subdomain}`' for subdomain in removed])
			self.notify(fields={'Removed subdomains': subdomains_str})

	if send_interesting and self.scan_id and self.domain_id:
		interesting_subdomains = get_interesting_subdomains(self.scan_id, self.domain_id)
		if interesting_subdomains:
			subdomains_str = '\n'.join([f'• `{subdomain}`' for subdomain in interesting_subdomains])
			self.notify(fields={'Interesting subdomains': subdomains_str})

	return SubdomainSerializer(subdomains, many=True).data


@app.task(base=RengineTask, bind=True)
def osint(self, host=None, ctx={}, description=None):
	"""Run Open-Source Intelligence tools on selected domain.

	Args:
		host (str): Hostname to scan.

	Returns:
		dict: Results from osint discovery and dorking.
	"""
	config = self.yaml_configuration.get(OSINT) or OSINT_DEFAULT_CONFIG
	results = {}

	if 'discover' in config:
		ctx['track'] = False
		results = osint_discovery(host=host, ctx=ctx)

	if 'dork' in config:
		ctx['track'] = False
		results['dorks'] = dorking(host=host, ctx=ctx)

	with open(self.output_path, 'w') as f:
		json.dump(results, f, indent=4)

	return results


@app.task(base=RengineTask, bind=True)
def osint_discovery(self, host=None, ctx={}):
	"""Run OSInt discovery.

	Args:
		host (str): Hostname to scan.

	Returns:
		dict: osint metadat and theHarvester and h8mail results.
	"""
	config = self.yaml_configuration.get(OSINT) or OSINT_DEFAULT_CONFIG
	osint_lookup = config.get(OSINT_DISCOVER, [])
	osint_intensity = config.get(INTENSITY, 'normal')
	documents_limit = config.get(OSINT_DOCUMENTS_LIMIT, 50)
	results = {}
	meta_info = []
	emails = []
	creds = []
	host = self.domain.name if self.domain else host

	# Get and save meta info
	if 'metainfo' in osint_lookup:
		if osint_intensity == 'normal':
			meta_dict = DottedDict({
				'osint_target': host,
				'domain': self.domain if self.domain else host,
				'scan_id': self.scan_id,
				'documents_limit': documents_limit
			})
			meta_info.append(save_metadata_info(meta_dict))
		elif osint_intensity == 'deep':
			subdomains = Subdomain.objects
			if self.scan:
				subdomains = subdomains.filter(scan_history=self.scan)
			for subdomain in subdomains:
				meta_dict = DottedDict({
					'osint_target': subdomain.name,
					'domain': self.domain,
					'scan_id': self.scan_id,
					'documents_limit': documents_limit
				})
				meta_info.append(save_metadata_info(meta_dict))

	if 'emails' in osint_lookup:
		emails = get_and_save_emails(self.scan, self.results_dir)
		emails_str = '\n'.join([f'• `{email}`' for email in emails])
		self.notify(fields={'Emails': emails_str})
		for email in emails:
			email, created = save_email(email, scan_history=self.scan)
			if created:
				logger.warning(f'Found new email address {email}')
		ctx['track'] = False
		creds = h8mail(ctx=ctx)

	if 'employees' in osint_lookup:
		ctx['track'] = False
		results = theHarvester(host=host, ctx=ctx)

	results['emails'] = results.get('emails', []) + emails
	results['creds'] = creds
	results['meta_info'] = meta_info
	return results


@app.task(base=RengineTask, bind=True)
def dorking(self, host=None, ctx={}):
	"""Run Google dorks.

	Args:
		host (str): Hostname to scan.

	Returns:
		list: Dorking results for each dork ran.
	"""
	# Some dork sources: https://github.com/six2dez/degoogle_hunter/blob/master/degoogle_hunter.sh
	config = self.yaml_configuration.get(OSINT) or OSINT_DEFAULT_CONFIG
	dorks = config.get(OSINT_DORK, [])
	results = []
	for dork in dorks:
		if dork == 'stackoverflow':
			dork_name = 'site:stackoverflow.com'
			dork_type = 'stackoverflow'
			results = get_and_save_dork_results(
				dork,
				dork_type,
				host=host,
				scan_history=self.scan,
				in_target=False)

		elif dork == '3rdparty' :
			# look in 3rd party sitee
			dork_type = '3rdparty'
			lookup_websites = [
				'gitter.im',
				'papaly.com',
				'productforums.google.com',
				'coggle.it',
				'replt.it',
				'ycombinator.com',
				'libraries.io',
				'npm.runkit.com',
				'npmjs.com',
				'scribd.com',
				'gitter.im'
			]
			dork_name = ''
			for website in lookup_websites:
				dork_name = dork + ' | ' + 'site:' + website
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=False)
				results.extend(tmp_results)

		elif dork == 'social_media' :
			dork_type = 'Social Media'
			social_websites = [
				'tiktok.com',
				'facebook.com',
				'twitter.com',
				'youtube.com',
				'pinterest.com',
				'tumblr.com',
				'reddit.com'
			]
			dork_name = ''
			for website in social_websites:
				dork_name = dork + ' | ' + 'site:' + website
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=False)
				results.extend(tmp_results)

		elif dork == 'project_management' :
			dork_type = 'Project Management'
			project_websites = [
				'trello.com',
				'*.atlassian.net'
			]
			dork_name = ''
			for website in project_websites:
				dork_name = dork + ' | ' + 'site:' + website
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=False)
				results.extend(tmp_results)

		elif dork == 'code_sharing' :
			dork_type = 'Code Sharing Sites'
			code_websites = [
				'github.com',
				'gitlab.com',
				'bitbucket.org'
			]
			dork_name = ''
			for website in code_websites:
				dork_name = dork + ' | ' + 'site:' + website
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=False)
				results.extend(tmp_results)

		elif dork == 'config_files' :
			dork_type = 'Config Files'
			config_file_ext = [
				'env',
				'xml',
				'conf',
				'cnf',
				'inf',
				'rdp',
				'ora',
				'txt',
				'cfg',
				'ini'
			]

			dork_name = ''
			results = []
			for extension in config_file_ext:
				dork_name = dork + ' | ' + 'ext:' + extension
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=True)
				results.extend(tmp_results)

		if dork == 'jenkins' :
			dork_type = 'Jenkins'
			dork_name = 'intitle:\"Dashboard [Jenkins]\"'
			tmp_results = get_and_save_dork_results(
				dork_name,
				dork_type,
				host=host,
				scan_history=self.scan,
				in_target=True)
			results.extend(tmp_results)

		elif dork == 'wordpress_files' :
			dork_type = 'Wordpress Files'
			inurl_lookup = [
				'wp-content',
				'wp-includes'
			]
			dork_name = ''
			for lookup in inurl_lookup:
				dork_name = dork + ' | ' + 'inurl:' + lookup
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=True)
				results.extend(tmp_results)

		elif dork == 'cloud_buckets':
			dork_type = 'Cloud Buckets'
			cloud_websites = [
				'.s3.amazonaws.com',
				'storage.googleapis.com',
				'amazonaws.com'
			]

			dork_name = ''
			for website in cloud_websites:
				dork_name = dork + ' | ' + 'site:' + website
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=False)
				results.extend(tmp_results)

		elif dork == 'php_error':
			dork_type = 'PHP Error'
			error_words = [
				'\"PHP Parse error\"',
				'\"PHP Warning\"',
				'\"PHP Error\"'
			]

			dork_name = ''
			for word in error_words:
				dork_name = dork + ' | ' + word
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=True)
				results.extend(tmp_results)

		elif dork == 'exposed_documents':
			dork_type = 'Exposed Documents'
			docs_file_ext = [
				'doc',
				'docx',
				'odt',
				'pdf',
				'rtf',
				'sxw',
				'psw',
				'ppt',
				'pptx',
				'pps',
				'csv'
			]

			dork_name = ''
			for extension in docs_file_ext:
				dork_name = dork + ' | ' + 'ext:' + extension
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=True)
				results.extend(tmp_results)

		elif dork == 'struts_rce':
			dork_type = 'Apache Struts RCE'
			struts_file_ext = [
				'action',
				'struts',
				'do'
			]

			dork_name = ''
			for extension in struts_file_ext:
				dork_name = dork + ' | ' + 'ext:' + extension
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=True)
				results.extend(tmp_results)

		elif dork == 'db_files':
			dork_type = 'Database Files'
			db_file_ext = [
				'sql',
				'db',
				'dbf',
				'mdb'
			]

			dork_name = ''
			for extension in db_file_ext:
				dork_name = dork_name + ' | ' + 'ext:' + extension
				tmp_results = get_and_save_dork_results(
					dork_name[3:],
					dork_type,
					host=host,
					scan_history=self.scan,
					in_target=True)
				results.extend(tmp_results)

		elif dork == 'traefik':
			dork_name = 'intitle:traefik inurl:8080/dashboard'
			dork_type = 'Traefik'
			tmp_results = get_and_save_dork_results(
				dork_name,
				dork_type,
				host=host,
				scan_history=self.scan,
				in_target=True)
			results.extend(tmp_results)

		elif dork == 'git_exposed':
			dork_name = 'inurl:\"/.git\"'
			dork_type = '.git Exposed'
			tmp_results = get_and_save_dork_results(
				dork_name,
				dork_type,
				host=host,
				scan_history=self.scan,
				in_target=True)
			results.extend(tmp_results)
	return results


@app.task(base=RengineTask, bind=True)
def theHarvester(self, host=None, ctx={}):
	"""Run theHarvester to get save emails, hosts, employees found in domain.

	Args:
		host (str): Hostname to scan.

	Returns:
		dict: Dict of emails, employees, hosts and ips found during crawling.
	"""
	config = self.yaml_configuration.get(OSINT) or {}
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)
	host = self.domain.name if self.domain else host
	if not host:
		logger.error('No host found in context.')
		return {}

	output_path_json = self.output_path.replace('.txt', '.json')
	theHarvester_dir = '/usr/src/github/theHarvester'
	history_file = f'{self.results_dir}/commands.txt'
	cmd  = f'python3 {theHarvester_dir}/theHarvester.py -d {host} -b all -f {output_path_json}'

	# Update proxies.yaml
	proxy_query = Proxy.objects.all()
	if proxy_query.exists():
		proxy = proxy_query.first()
		if proxy.use_proxy:
			proxy_list = proxy.proxies.splitlines()
			yaml_data = {'http' : proxy_list}
			with open(f'{theHarvester_dir}/proxies.yaml', 'w') as file:
				yaml.dump(yaml_data, file)

	# Run cmd
	run_command(
		cmd,
		shell=False,
		cwd=theHarvester_dir,
		history_file=history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)

	# Get file location
	if not os.path.isfile(output_path_json):
		logger.error(f'Could not open {output_path_json}')
		return {}

	# Load theHarvester results
	with open(output_path_json, 'r') as f:
		data = json.load(f)

	# Re-indent theHarvester JSON
	with open(output_path_json, 'w') as f:
		json.dump(data, f, indent=4)

	emails = data.get('emails', [])
	for email_address in emails:
		email, _ = save_email(email_address, scan_history=self.scan)
		if email:
			self.notify(fields={'Emails': f'• `{email.address}`'})

	linkedin_people = data.get('linkedin_people', [])
	for people in linkedin_people:
		employee, _ = save_employee(
			people,
			designation='linkedin',
			scan_history=self.scan)
		if employee:
			self.notify(fields={'LinkedIn people': f'• {employee.name}'})

	twitter_people = data.get('twitter_people', [])
	for people in twitter_people:
		employee, _ = save_employee(
			people,
			designation='twitter',
			scan_history=self.scan)
		if employee:
			self.notify(fields={'Twitter people': f'• {employee.name}'})

	hosts = data.get('hosts', [])
	urls = []
	for host in hosts:
		split = tuple(host.split(':'))
		http_url = split[0]
		subdomain_name = get_subdomain_from_url(http_url)
		subdomain, _ = save_subdomain(subdomain_name, ctx=ctx)
		endpoint, _ = save_endpoint(
			http_url,
			crawl=False,
			ctx=ctx,
			subdomain=subdomain)
		if endpoint:
			urls.append(endpoint.http_url)
			self.notify(fields={'Hosts': f'• {endpoint.http_url}'})

	if enable_http_crawl:
		ctx['track'] = False
		http_crawl(urls, ctx=ctx)

	# TODO: Lots of ips unrelated with our domain are found, disabling
	# this for now.
	# ips = data.get('ips', [])
	# for ip_address in ips:
	# 	ip, created = save_ip_address(
	# 		ip_address,
	# 		subscan=subscan)
	# 	if ip:
	# 		send_task_notif.delay(
	# 			'osint',
	# 			scan_history_id=scan_history_id,
	# 			subscan_id=subscan_id,
	# 			severity='success',
	# 			update_fields={'IPs': f'{ip.address}'})
	return data


@app.task(base=RengineTask, bind=True)
def h8mail(self, input_path=None, ctx={}):
	"""Run h8mail.

	Args:
		input_path (str): Emails input file.

	Returns:
		list[dict]: List of credentials info.
	"""
	logger.warning('Getting leaked credentials')
	results_dir = self.results_dir
	scan = self.scan
	input_path = input_path if input_path else f'{self.results_dir}/emails.txt'
	output_path = self.output_path
	cmd = f'h8mail -t {input_path} --json {output_path}'
	history_file = f'{results_dir}/commands.txt'

	run_command(
		cmd,
		history_file=history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)

	with open(output_path) as f:
		data = json.load(f)
		creds = data.get('targets', [])

	# TODO: go through h8mail output and save emails to DB
	for cred in creds:
		logger.warning(cred)
		email_address = cred['target']
		pwn_num = cred['pwn_num']
		pwn_data = cred.get('data', [])
		email, created = save_email(email_address, scan_history=scan)
		if email:
			self.notify(fields={'Emails': f'• `{email.address}`'})
	return creds


@app.task(base=RengineTask, bind=True)
def screenshot(self, ctx={}, description=None):
	"""Uses EyeWitness to gather screenshot of a domain and/or url.

	Args:
		description (str, optional): Task description shown in UI.
	"""

	# Config
	screenshots_path = f'{self.results_dir}/screenshots'
	output_path = f'{self.results_dir}/screenshots/{self.filename}'
	alive_endpoints_file = f'{self.results_dir}/endpoints_alive.txt'
	config = self.yaml_configuration.get(SCREENSHOT) or {}
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)
	intensity = config.get(INTENSITY) or self.yaml_configuration.get(INTENSITY, DEFAULT_SCAN_INTENSITY)
	timeout = config.get(TIMEOUT) or self.yaml_configuration.get(TIMEOUT, DEFAULT_HTTP_TIMEOUT + 5)
	threads = config.get(THREADS) or self.yaml_configuration.get(THREADS, DEFAULT_THREADS)

	# If intensity is normal, grab only the root endpoints of each subdomain
	strict = True if intensity == 'normal' else False

	# Get URLs to take screenshot of
	get_http_urls(
		is_alive=enable_http_crawl,
		strict=strict,
		write_filepath=alive_endpoints_file,
		get_only_default_urls=True,
		ctx=ctx
	)

	# Send start notif
	notification = Notification.objects.first()
	send_output_file = notification.send_scan_output_file if notification else False

	# Run cmd
	cmd = f'python3 /usr/src/github/EyeWitness/Python/EyeWitness.py -f {alive_endpoints_file} -d {screenshots_path} --no-prompt'
	cmd += f' --timeout {timeout}' if timeout > 0 else ''
	cmd += f' --threads {threads}' if threads > 0 else ''
	run_command(
		cmd,
		shell=False,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)
	if not os.path.isfile(output_path):
		logger.error(f'Could not load EyeWitness results at {output_path} for {self.domain.name}.')
		return

	# Loop through results and save objects in DB
	screenshot_paths = []
	with open(output_path, 'r') as file:
		reader = csv.reader(file)
		for row in reader:
			"Protocol,Port,Domain,Request Status,Screenshot Path, Source Path"
			protocol, port, subdomain_name, status, screenshot_path, source_path = tuple(row)
			logger.info(f'{protocol}:{port}:{subdomain_name}:{status}')
			subdomain_query = Subdomain.objects.filter(name=subdomain_name)
			if self.scan:
				subdomain_query = subdomain_query.filter(scan_history=self.scan)
			if status == 'Successful' and subdomain_query.exists():
				subdomain = subdomain_query.first()
				screenshot_paths.append(screenshot_path)
				subdomain.screenshot_path = screenshot_path.replace('/usr/src/scan_results/', '')
				subdomain.save()
				logger.warning(f'Added screenshot for {subdomain.name} to DB')

	# Remove all db, html extra files in screenshot results
	run_command(
		'rm -rf {0}/*.csv {0}/*.db {0}/*.js {0}/*.html {0}/*.css'.format(screenshots_path),
		shell=True,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)
	run_command(
		f'rm -rf {screenshots_path}/source',
		shell=True,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)

	# Send finish notifs
	screenshots_str = '• ' + '\n• '.join([f'`{path}`' for path in screenshot_paths])
	self.notify(fields={'Screenshots': screenshots_str})
	if send_output_file:
		for path in screenshot_paths:
			title = get_output_file_name(
				self.scan_id,
				self.subscan_id,
				self.filename)
			send_file_to_discord.delay(path, title)


@app.task(base=RengineTask, bind=True)
def port_scan(self, hosts=[], ctx={}, description=None):
	"""Run port scan.

	Args:
		hosts (list, optional): Hosts to run port scan on.
		description (str, optional): Task description shown in UI.

	Returns:
		list: List of open ports (dict).
	"""
	input_file = f'{self.results_dir}/input_subdomains_port_scan.txt'
	proxy = get_random_proxy()

	# Config
	config = self.yaml_configuration.get(PORT_SCAN) or {}
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)
	intensity = config.get(INTENSITY) or self.yaml_configuration.get(INTENSITY, DEFAULT_SCAN_INTENSITY)
	timeout = config.get(TIMEOUT) or self.yaml_configuration.get(TIMEOUT, DEFAULT_HTTP_TIMEOUT)
	exclude_ports = config.get(NAABU_EXCLUDE_PORTS, [])
	exclude_subdomains = config.get(NAABU_EXCLUDE_SUBDOMAINS, False)
	ports = config.get(PORTS, NAABU_DEFAULT_PORTS)
	ports = [str(port) for port in ports]
	rate_limit = config.get(NAABU_RATE) or self.yaml_configuration.get(RATE_LIMIT, DEFAULT_RATE_LIMIT)
	threads = config.get(THREADS) or self.yaml_configuration.get(THREADS, DEFAULT_THREADS)
	passive = config.get(NAABU_PASSIVE, False)
	use_naabu_config = config.get(USE_NAABU_CONFIG, False)
	exclude_ports_str = ','.join(exclude_ports)
	# nmap args
	nmap_enabled = config.get(ENABLE_NMAP, False)
	nmap_cmd = config.get(NMAP_COMMAND, '')
	nmap_script = config.get(NMAP_SCRIPT, '')
	nmap_script = ','.join(nmap_script)
	nmap_script_args = config.get(NMAP_SCRIPT_ARGS)

	if hosts:
		with open(input_file, 'w') as f:
			f.write('\n'.join(hosts))
	else:
		hosts = get_subdomains(
			write_filepath=input_file,
			exclude_subdomains=exclude_subdomains,
			ctx=ctx)

	# Build cmd
	cmd = 'naabu -json -exclude-cdn'
	cmd += f' -list {input_file}' if len(hosts) > 0 else f' -host {hosts[0]}'
	if 'full' in ports or 'all' in ports:
		ports_str = ' -p "-"'
	elif 'top-100' in ports:
		ports_str = ' -top-ports 100'
	elif 'top-1000' in ports:
		ports_str = ' -top-ports 1000'
	else:
		ports_str = ','.join(ports)
		ports_str = f' -p {ports_str}'
	cmd += ports_str
	cmd += ' -config /root/.config/naabu/config.yaml' if use_naabu_config else ''
	cmd += f' -proxy "{proxy}"' if proxy else ''
	cmd += f' -c {threads}' if threads else ''
	cmd += f' -rate {rate_limit}' if rate_limit > 0 else ''
	cmd += f' -timeout {timeout*1000}' if timeout > 0 else ''
	cmd += f' -passive' if passive else ''
	cmd += f' -exclude-ports {exclude_ports_str}' if exclude_ports else ''
	cmd += f' -silent'

	# Execute cmd and gather results
	results = []
	urls = []
	ports_data = {}
	for line in stream_command(
			cmd,
			shell=True,
			history_file=self.history_file,
			scan_id=self.scan_id,
			activity_id=self.activity_id):

		if not isinstance(line, dict):
			continue
		results.append(line)
		port_number = line['port']['Port']
		ip_address = line['ip']
		host = line.get('host') or ip_address
		if port_number == 0:
			continue

		# Grab subdomain
		subdomain = Subdomain.objects.filter(
			name=host,
			target_domain=self.domain,
			scan_history=self.scan
		).first()

		# Add IP DB
		ip, _ = save_ip_address(ip_address, subdomain, subscan=self.subscan)
		if self.subscan:
			ip.ip_subscan_ids.add(self.subscan)
			ip.save()

		# Add endpoint to DB
		# port 80 and 443 not needed as http crawl already does that.
		if port_number not in [80, 443]:
			http_url = f'{host}:{port_number}'
			endpoint, _ = save_endpoint(
				http_url,
				crawl=enable_http_crawl,
				ctx=ctx,
				subdomain=subdomain)
			if endpoint:
				http_url = endpoint.http_url
			urls.append(http_url)

		# Add Port in DB
		port_details = whatportis.get_ports(str(port_number))
		service_name = port_details[0].name if len(port_details) > 0 else 'unknown'
		description = port_details[0].description if len(port_details) > 0 else ''

		# get or create port
		port, created = Port.objects.get_or_create(
			number=port_number,
			service_name=service_name,
			description=description
		)
		if port_number in UNCOMMON_WEB_PORTS:
			port.is_uncommon = True
			port.save()
		ip.ports.add(port)
		ip.save()
		if host in ports_data:
			ports_data[host].append(port_number)
		else:
			ports_data[host] = [port_number]

		# Send notification
		logger.warning(f'Found opened port {port_number} on {ip_address} ({host})')

	# Send notification
	fields_str = ''
	for host, ports in ports_data.items():
		ports_str = ', '.join([f'`{port}`' for port in ports])
		fields_str += f'• `{host}`: {ports_str}\n'
	self.notify(fields={'Ports discovered': fields_str})

	# Save output to file
	with open(self.output_path, 'w') as f:
		json.dump(results, f, indent=4)

	logger.info('Finished running naabu port scan.')

	# Process nmap results: 1 process per host
	sigs = []
	if nmap_enabled:
		logger.warning(f'Starting nmap scans ...')
		logger.warning(ports_data)
		for host, port_list in ports_data.items():
			ports_str = '_'.join([str(p) for p in port_list])
			ctx_nmap = ctx.copy()
			ctx_nmap['description'] = get_task_title(f'nmap_{host}', self.scan_id, self.subscan_id)
			ctx_nmap['track'] = False
			sig = nmap.si(
				cmd=nmap_cmd,
				ports=port_list,
				host=host,
				script=nmap_script,
				script_args=nmap_script_args,
				max_rate=rate_limit,
				ctx=ctx_nmap)
			sigs.append(sig)
		task = group(sigs).apply_async()
		with allow_join_result():
			results = task.get()

	return ports_data


@app.task(base=RengineTask, bind=True)
def nmap(
		self,
		cmd=None,
		ports=[],
		host=None,
		input_file=None,
		script=None,
		script_args=None,
		max_rate=None,
		ctx={},
		description=None):
	"""Run nmap on a host.

	Args:
		cmd (str, optional): Existing nmap command to complete.
		ports (list, optional): List of ports to scan.
		host (str, optional): Host to scan.
		input_file (str, optional): Input hosts file.
		script (str, optional): NSE script to run.
		script_args (str, optional): NSE script args.
		max_rate (int): Max rate.
		description (str, optional): Task description shown in UI.
	"""
	notif = Notification.objects.first()
	ports_str = ','.join(str(port) for port in ports)
	self.filename = self.filename.replace('.txt', '.xml')
	filename_vulns = self.filename.replace('.xml', '_vulns.json')
	output_file = self.output_path
	output_file_xml = f'{self.results_dir}/{host}_{self.filename}'
	vulns_file = f'{self.results_dir}/{host}_{filename_vulns}'
	logger.warning(f'Running nmap on {host}:{ports}')

	# Build cmd
	nmap_cmd = get_nmap_cmd(
		cmd=cmd,
		ports=ports_str,
		script=script,
		script_args=script_args,
		max_rate=max_rate,
		host=host,
		input_file=input_file,
		output_file=output_file_xml)

	# Run cmd
	run_command(
		nmap_cmd,
		shell=True,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)

	# Get nmap XML results and convert to JSON
	vulns = parse_nmap_results(output_file_xml, output_file)
	with open(vulns_file, 'w') as f:
		json.dump(vulns, f, indent=4)

	# Save vulnerabilities found by nmap
	vulns_str = ''
	for vuln_data in vulns:
		# URL is not necessarily an HTTP URL when running nmap (can be any
		# other vulnerable protocols). Look for existing endpoint and use its
		# URL as vulnerability.http_url if it exists.
		url = vuln_data['http_url']
		endpoint = EndPoint.objects.filter(http_url__contains=url).first()
		if endpoint:
			vuln_data['http_url'] = endpoint.http_url
		vuln, created = save_vulnerability(
			target_domain=self.domain,
			subdomain=self.subdomain,
			scan_history=self.scan,
			subscan=self.subscan,
			endpoint=endpoint,
			**vuln_data)
		vulns_str += f'• {str(vuln)}\n'
		if created:
			logger.warning(str(vuln))

	# Send only 1 notif for all vulns to reduce number of notifs
	if notif and notif.send_vuln_notif and vulns_str:
		logger.warning(vulns_str)
		self.notify(fields={'CVEs': vulns_str})
	return vulns


@app.task(base=RengineTask, bind=True)
def waf_detection(self, ctx={}, description=None):
	"""
	Uses wafw00f to check for the presence of a WAF.

	Args:
		description (str, optional): Task description shown in UI.

	Returns:
		list: List of startScan.models.Waf objects.
	"""
	input_path = f'{self.results_dir}/input_endpoints_waf_detection.txt'
	config = self.yaml_configuration.get(WAF_DETECTION) or {}
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)

	# Get alive endpoints from DB
	get_http_urls(
		is_alive=enable_http_crawl,
		write_filepath=input_path,
		get_only_default_urls=True,
		ctx=ctx
	)

	cmd = f'wafw00f -i {input_path} -o {self.output_path}'
	run_command(
		cmd,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)
	if not os.path.isfile(self.output_path):
		logger.error(f'Could not find {self.output_path}')
		return

	with open(self.output_path) as file:
		wafs = file.readlines()

	for line in wafs:
		line = " ".join(line.split())
		splitted = line.split(' ', 1)
		waf_info = splitted[1].strip()
		waf_name = waf_info[:waf_info.find('(')].strip()
		waf_manufacturer = waf_info[waf_info.find('(')+1:waf_info.find(')')].strip().replace('.', '')
		http_url = sanitize_url(splitted[0].strip())
		if not waf_name or waf_name == 'None':
			continue

		# Add waf to db
		waf, _ = Waf.objects.get_or_create(
			name=waf_name,
			manufacturer=waf_manufacturer
		)

		# Add waf info to Subdomain in DB
		subdomain = get_subdomain_from_url(http_url)
		logger.info(f'Wafw00f Subdomain : {subdomain}')
		subdomain_query, _ = Subdomain.objects.get_or_create(scan_history=self.scan, name=subdomain)
		subdomain_query.waf.add(waf)
		subdomain_query.save()
	return wafs


@app.task(base=RengineTask, bind=True)
def dir_file_fuzz(self, ctx={}, description=None):
	"""Perform directory scan, and currently uses `ffuf` as a default tool.

	Args:
		description (str, optional): Task description shown in UI.

	Returns:
		list: List of URLs discovered.
	"""
	# Config
	cmd = 'ffuf'
	config = self.yaml_configuration.get(DIR_FILE_FUZZ) or {}
	custom_header = self.yaml_configuration.get(CUSTOM_HEADER)
	auto_calibration = config.get(AUTO_CALIBRATION, True)
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)
	intensity = config.get(INTENSITY) or self.yaml_configuration.get(INTENSITY, DEFAULT_SCAN_INTENSITY)
	rate_limit = config.get(RATE_LIMIT) or self.yaml_configuration.get(RATE_LIMIT, DEFAULT_RATE_LIMIT)
	extensions = config.get(EXTENSIONS, DEFAULT_DIR_FILE_FUZZ_EXTENSIONS)
	extensions_str = ','.join(map(str, extensions))
	follow_redirect = config.get(FOLLOW_REDIRECT, FFUF_DEFAULT_FOLLOW_REDIRECT)
	max_time = config.get(MAX_TIME, 0)
	match_http_status = config.get(MATCH_HTTP_STATUS, FFUF_DEFAULT_MATCH_HTTP_STATUS)
	mc = ','.join([str(c) for c in match_http_status])
	recursive_level = config.get(RECURSIVE_LEVEL, FFUF_DEFAULT_RECURSIVE_LEVEL)
	stop_on_error = config.get(STOP_ON_ERROR, False)
	timeout = config.get(TIMEOUT) or self.yaml_configuration.get(TIMEOUT, DEFAULT_HTTP_TIMEOUT)
	threads = config.get(THREADS) or self.yaml_configuration.get(THREADS, DEFAULT_THREADS)
	wordlist_name = config.get(WORDLIST, 'dicc')
	delay = rate_limit / (threads * 100) # calculate request pause delay from rate_limit and number of threads
	input_path = f'{self.results_dir}/input_dir_file_fuzz.txt'

	# Get wordlist
	wordlist_name = 'dicc' if wordlist_name == 'default' else wordlist_name
	wordlist_path = f'/usr/src/wordlist/{wordlist_name}.txt'

	# Build command
	cmd += f' -w {wordlist_path}'
	cmd += f' -e {extensions_str}' if extensions else ''
	cmd += f' -maxtime {max_time}' if max_time > 0 else ''
	cmd += f' -p {delay}' if delay > 0 else ''
	cmd += f' -recursion -recursion-depth {recursive_level} ' if recursive_level > 0 else ''
	cmd += f' -t {threads}' if threads and threads > 0 else ''
	cmd += f' -timeout {timeout}' if timeout and timeout > 0 else ''
	cmd += ' -se' if stop_on_error else ''
	cmd += ' -fr' if follow_redirect else ''
	cmd += ' -ac' if auto_calibration else ''
	cmd += f' -mc {mc}' if mc else ''
	cmd += f' -H "{custom_header}"' if custom_header else ''

	# Grab URLs to fuzz
	urls = get_http_urls(
		is_alive=True,
		ignore_files=False,
		write_filepath=input_path,
		get_only_default_urls=True,
		ctx=ctx
	)
	logger.warning(urls)

	# Loop through URLs and run command
	results = []
	for url in urls:
		'''
			Above while fetching urls, we are not ignoring files, because some
			default urls may redirect to https://example.com/login.php
			so, ignore_files is set to False
			but, during fuzzing, we will only need part of the path, in above example
			it is still a good idea to ffuf base url https://example.com
			so files from base url
		'''
		url_parse = urlparse(url)
		url = url_parse.scheme + '://' + url_parse.netloc
		url += '/FUZZ' # TODO: fuzz not only URL but also POST / PUT / headers
		proxy = get_random_proxy()

		# Build final cmd
		fcmd = cmd
		fcmd += f' -x {proxy}' if proxy else ''
		fcmd += f' -u {url} -json'

		# Initialize DirectoryScan object
		dirscan = DirectoryScan()
		dirscan.scanned_date = timezone.now()
		dirscan.command_line = fcmd
		dirscan.save()

		# Loop through results and populate EndPoint and DirectoryFile in DB
		results = []
		for line in stream_command(
				fcmd,
				shell=True,
				history_file=self.history_file,
				scan_id=self.scan_id,
				activity_id=self.activity_id):
			if not isinstance(line, dict):
				continue
			results.append(line)
			name = line['input'].get('FUZZ')
			length = line['length']
			status = line['status']
			words = line['words']
			url = line['url']
			lines = line['lines']
			content_type = line['content-type']
			duration = line['duration']
			if not name:
				logger.error(f'FUZZ not found for "{url}"')
				continue
			endpoint, created = save_endpoint(url, crawl=False, ctx=ctx)
			# endpoint.is_default = False
			endpoint.http_status = status
			endpoint.content_length = length
			endpoint.response_time = duration / 1000000000
			endpoint.save()
			if created:
				urls.append(endpoint.http_url)
			endpoint.status = status
			endpoint.content_type = content_type
			endpoint.content_length = length
			dfile, created = DirectoryFile.objects.get_or_create(
				name=name,
				length=length,
				words=words,
				lines=lines,
				content_type=content_type,
				url=url)
			dfile.http_status = status
			dfile.save()
			if created:
				logger.warning(f'Found new directory or file {url}')
			dirscan.directory_files.add(dfile)
			dirscan.save()

			if self.subscan:
				dirscan.dir_subscan_ids.add(self.subscan)

			subdomain_name = get_subdomain_from_url(endpoint.http_url)
			subdomain = Subdomain.objects.get(name=subdomain_name, scan_history=self.scan)
			subdomain.directories.add(dirscan)
			subdomain.save()

	# Crawl discovered URLs
	if enable_http_crawl:
		ctx['track'] = False
		http_crawl(urls, ctx=ctx)

	return results


@app.task(base=RengineTask, bind=True)
def fetch_url(self, urls=[], ctx={}, description=None):
	"""Fetch URLs using different tools like gauplus, gospider, waybackurls ...

	Args:
		urls (list): List of URLs to start from.
		description (str, optional): Task description shown in UI.
	"""
	input_path = f'{self.results_dir}/input_endpoints_fetch_url.txt'
	proxy = get_random_proxy()

	# Config
	config = self.yaml_configuration.get(FETCH_URL) or {}
	should_remove_duplicate_endpoints = config.get(REMOVE_DUPLICATE_ENDPOINTS, True)
	duplicate_removal_fields = config.get(DUPLICATE_REMOVAL_FIELDS, ENDPOINT_SCAN_DEFAULT_DUPLICATE_FIELDS)
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)
	gf_patterns = config.get(GF_PATTERNS, DEFAULT_GF_PATTERNS)
	ignore_file_extension = config.get(IGNORE_FILE_EXTENSION, DEFAULT_IGNORE_FILE_EXTENSIONS)
	tools = config.get(USES_TOOLS, ENDPOINT_SCAN_DEFAULT_TOOLS)
	threads = config.get(THREADS) or self.yaml_configuration.get(THREADS, DEFAULT_THREADS)
	domain_request_headers = self.domain.request_headers if self.domain else None
	custom_header = domain_request_headers or self.yaml_configuration.get(CUSTOM_HEADER)
	exclude_subdomains = config.get('exclude_subdomains', False)

	# Get URLs to scan and save to input file
	if urls:
		with open(input_path, 'w') as f:
			f.write('\n'.join(urls))
	else:
		urls = get_http_urls(
			is_alive=enable_http_crawl,
			write_filepath=input_path,
			exclude_subdomains=exclude_subdomains,
			get_only_default_urls=True,
			ctx=ctx
		)

	# Domain regex
	host = self.domain.name if self.domain else urlparse(urls[0]).netloc
	host_regex = f"\'https?://([a-z0-9]+[.])*{host}.*\'"

	# Tools cmds
	cmd_map = {
		'gauplus': f'gauplus --random-agent -t {threads}',
		'hakrawler': 'hakrawler -subs -u',
		'waybackurls': 'waybackurls',
		'gospider': f'gospider -S {input_path} --js -d 2 --sitemap --robots -w -r',
		'katana': f'katana -list {input_path} -silent -jc -kf all -d 3 -fs rdn',
	}
	if proxy:
		cmd_map['gauplus'] += f' -p "{proxy}"'
		cmd_map['gospider'] += f' -p {proxy}'
		cmd_map['hakrawler'] += f' -proxy {proxy}'
		cmd_map['katana'] += f' -proxy {proxy}'
	if threads > 0:
		cmd_map['gauplus'] += f' -t {threads}'
		cmd_map['gospider'] += f' -t {threads}'
		cmd_map['katana'] += f' -c {threads}'
	if custom_header:
		header_string = ';;'.join([
			f'{key}: {value}' for key, value in custom_header.items()
		])
		cmd_map['hakrawler'] += f' -h {header_string}'
		cmd_map['katana'] += f' -H {header_string}'
		header_flags = [':'.join(h) for h in header_string.split(';;')]
		for flag in header_flags:
			cmd_map['gospider'] += f' -H {flag}'
	cat_input = f'cat {input_path}'
	grep_output = f'grep -Eo {host_regex}'
	cmd_map = {
		tool: f'{cat_input} | {cmd} | {grep_output} > {self.results_dir}/urls_{tool}.txt'
		for tool, cmd in cmd_map.items()
	}
	tasks = group(
		run_command.si(
			cmd,
			shell=True,
			scan_id=self.scan_id,
			activity_id=self.activity_id)
		for tool, cmd in cmd_map.items()
		if tool in tools
	)

	# Cleanup task
	sort_output = [
		f'cat {self.results_dir}/urls_* > {self.output_path}',
		f'cat {input_path} >> {self.output_path}',
		f'sort -u {self.output_path} -o {self.output_path}',
	]
	if ignore_file_extension:
		ignore_exts = '|'.join(ignore_file_extension)
		grep_ext_filtered_output = [
			f'cat {self.output_path} | grep -Eiv "\\.({ignore_exts}).*" > {self.results_dir}/urls_filtered.txt',
			f'mv {self.results_dir}/urls_filtered.txt {self.output_path}'
		]
		sort_output.extend(grep_ext_filtered_output)
	cleanup = chain(
		run_command.si(
			cmd,
			shell=True,
			scan_id=self.scan_id,
			activity_id=self.activity_id)
		for cmd in sort_output
	)

	# Run all commands
	task = chord(tasks)(cleanup)
	with allow_join_result():
		task.get()

	# Store all the endpoints and run httpx
	with open(self.output_path) as f:
		discovered_urls = f.readlines()
		self.notify(fields={'Discovered URLs': len(discovered_urls)})

	# Some tools can have an URL in the format <URL>] - <PATH> or <URL> - <PATH>, add them
	# to the final URL list
	all_urls = []
	for url in discovered_urls:
		url = url.strip()
		urlpath = None
		base_url = None
		if '] ' in url: # found JS scraped endpoint e.g from gospider
			split = tuple(url.split('] '))
			if not len(split) == 2:
				logger.warning(f'URL format not recognized for "{url}". Skipping.')
				continue
			base_url, urlpath = split
			urlpath = urlpath.lstrip('- ')
		elif ' - ' in url: # found JS scraped endpoint e.g from gospider
			base_url, urlpath = tuple(url.split(' - '))

		if base_url and urlpath:
			subdomain = urlparse(base_url)
			url = f'{subdomain.scheme}://{subdomain.netloc}{self.url_filter}'

		if not validators.url(url):
			logger.warning(f'Invalid URL "{url}". Skipping.')

		if url not in all_urls:
			all_urls.append(url)

	# Filter out URLs if a path filter was passed
	if self.url_filter:
		all_urls = [url for url in all_urls if self.url_filter in url]

	# Write result to output path
	with open(self.output_path, 'w') as f:
		f.write('\n'.join(all_urls))
	logger.warning(f'Found {len(all_urls)} usable URLs')

	# Crawl discovered URLs
	if enable_http_crawl:
		ctx['track'] = False
		http_crawl(
			all_urls,
			ctx=ctx,
			should_remove_duplicate_endpoints=should_remove_duplicate_endpoints,
			duplicate_removal_fields=duplicate_removal_fields
		)


	#-------------------#
	# GF PATTERNS MATCH #
	#-------------------#

	# Combine old gf patterns with new ones
	if gf_patterns:
		self.scan.used_gf_patterns = ','.join(gf_patterns)
		self.scan.save()

	# Run gf patterns on saved endpoints
	# TODO: refactor to Celery task
	for gf_pattern in gf_patterns:
		# TODO: js var is causing issues, removing for now
		if gf_pattern == 'jsvar':
			logger.info('Ignoring jsvar as it is causing issues.')
			continue

		# Run gf on current pattern
		logger.warning(f'Running gf on pattern "{gf_pattern}"')
		gf_output_file = f'{self.results_dir}/gf_patterns_{gf_pattern}.txt'
		cmd = f'cat {self.output_path} | gf {gf_pattern} | grep -Eo {host_regex} >> {gf_output_file}'
		run_command(
			cmd,
			shell=True,
			history_file=self.history_file,
			scan_id=self.scan_id,
			activity_id=self.activity_id)

		# Check output file
		if not os.path.exists(gf_output_file):
			logger.error(f'Could not find GF output file {gf_output_file}. Skipping GF pattern "{gf_pattern}"')
			continue

		# Read output file line by line and
		with open(gf_output_file, 'r') as f:
			lines = f.readlines()

		# Add endpoints / subdomains to DB
		for url in lines:
			http_url = sanitize_url(url)
			subdomain_name = get_subdomain_from_url(http_url)
			subdomain, _ = save_subdomain(subdomain_name, ctx=ctx)
			if not subdomain:
				continue
			endpoint, created = save_endpoint(
				http_url,
				crawl=False,
				subdomain=subdomain,
				ctx=ctx)
			earlier_pattern = None
			if not created:
				earlier_pattern = endpoint.matched_gf_patterns
			pattern = f'{earlier_pattern},{gf_pattern}' if earlier_pattern else gf_pattern
			endpoint.matched_gf_patterns = pattern
			endpoint.save()

	return all_urls


def parse_curl_output(response):
	# TODO: Enrich from other cURL fields.
	CURL_REGEX_HTTP_STATUS = f'HTTP\/(?:(?:\d\.?)+)\s(\d+)\s(?:\w+)'
	http_status = 0
	if response:
		failed = False
		regex = re.compile(CURL_REGEX_HTTP_STATUS, re.MULTILINE)
		try:
			http_status = int(regex.findall(response)[0])
		except (KeyError, TypeError, IndexError):
			pass
	return {
		'http_status': http_status,
	}


@app.task(base=RengineTask, bind=True)
def vulnerability_scan(self, urls=[], ctx={}, description=None):
	"""HTTP vulnerability scan using `nuclei`.

	Args:
		urls (list, optional): If passed, filter on those URLs.
		description (str, optional): Task description shown in UI.

	Notes:
	Unfurl the urls to keep only domain and path, will be sent to vuln scan and
	ignore certain file extensions. Thanks: https://github.com/six2dez/reconftw
	"""
	# Config
	config = self.yaml_configuration.get(VULNERABILITY_SCAN) or {}
	input_path = f'{self.results_dir}/input_endpoints_vulnerability_scan.txt'
	enable_http_crawl = config.get(ENABLE_HTTP_CRAWL, DEFAULT_ENABLE_HTTP_CRAWL)
	concurrency = config.get(NUCLEI_CONCURRENCY) or self.yaml_configuration.get(THREADS, DEFAULT_THREADS)
	intensity = config.get(INTENSITY) or self.yaml_configuration.get(INTENSITY, DEFAULT_SCAN_INTENSITY)
	rate_limit = config.get(RATE_LIMIT) or self.yaml_configuration.get(RATE_LIMIT, DEFAULT_RATE_LIMIT)
	retries = config.get(RETRIES) or self.yaml_configuration.get(RETRIES, DEFAULT_RETRIES)
	timeout = config.get(TIMEOUT) or self.yaml_configuration.get(TIMEOUT, DEFAULT_HTTP_TIMEOUT)
	custom_header = config.get(CUSTOM_HEADER) or self.yaml_configuration.get(CUSTOM_HEADER)
	tags = config.get(NUCLEI_TAGS, [])
	tags = ','.join(tags)
	nuclei_templates = config.get(NUCLEI_TEMPLATE)
	custom_nuclei_templates = config.get(NUCLEI_CUSTOM_TEMPLATE)
	proxy = get_random_proxy()
	use_nuclei_conf = config.get(USE_NUCLEI_CONFIG, False)
	severities = config.get(NUCLEI_SEVERITY, NUCLEI_DEFAULT_SEVERITIES)
	severities_str = ','.join(severities)

	# Get alive endpoints
	if urls:
		with open(input_path, 'w') as f:
			f.write('\n'.join(urls))
	else:
		get_http_urls(
			is_alive=enable_http_crawl,
			ignore_files=True,
			write_filepath=input_path,
			ctx=ctx
		)

	if intensity == 'normal': # reduce number of endpoints to scan
		unfurl_filter = f'{self.results_dir}/urls_unfurled.txt'
		run_command(
			f'cat {input_path} | unfurl -u format %s://%d%p > {unfurl_filter}',
			shell=True,
			history_file=self.history_file,
			scan_id=self.scan_id,
			activity_id=self.activity_id)
		run_command(
			f'sort -u {unfurl_filter} -o  {unfurl_filter}',
			shell=True,
			history_file=self.history_file,
			scan_id=self.scan_id,
			activity_id=self.activity_id)
		input_path = unfurl_filter

	# Send start notification
	notif = Notification.objects.first()
	send_status = notif.send_scan_status_notif if notif else False

	# Build templates
	# logger.info('Updating Nuclei templates ...')
	run_command(
		'nuclei -update-templates',
		shell=True,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)
	templates = []
	if not (nuclei_templates or custom_nuclei_templates):
		templates.append(NUCLEI_DEFAULT_TEMPLATES_PATH)

	if nuclei_templates:
		if ALL in nuclei_templates:
			template = NUCLEI_DEFAULT_TEMPLATES_PATH
			templates.append(template)
		else:
			templates.extend(nuclei_templates)

	if custom_nuclei_templates:
		custom_nuclei_template_paths = [f'{str(elem)}.yaml' for elem in custom_nuclei_templates]
		template = templates.extend(custom_nuclei_template_paths)

	# Build CMD
	cmd = 'nuclei -j'
	cmd += ' -config /root/.config/nuclei/config.yaml' if use_nuclei_conf else ''
	cmd += f' -irr'
	cmd += f' -H "{custom_header}"' if custom_header else ''
	cmd += f' -l {input_path}'
	cmd += f' -c {str(concurrency)}' if concurrency > 0 else ''
	cmd += f' -proxy {proxy} ' if proxy else ''
	cmd += f' -retries {retries}' if retries > 0 else ''
	cmd += f' -rl {rate_limit}' if rate_limit > 0 else ''
	cmd += f' -severity {severities_str}'
	cmd += f' -timeout {str(timeout)}' if timeout and timeout > 0 else ''
	cmd += f' -tags {tags}' if tags else ''
	cmd += f' -silent'
	for tpl in templates:
		cmd += f' -t {tpl}'

	# Run cmd
	results = []
	for line in stream_command(
			cmd,
			history_file=self.history_file,
			scan_id=self.scan_id,
			activity_id=self.activity_id):

		if not isinstance(line, dict):
			continue

		results.append(line)

		# Gather nuclei results
		vuln_data = parse_nuclei_result(line)

		# Get corresponding subdomain
		http_url = sanitize_url(line.get('matched-at'))
		subdomain_name = get_subdomain_from_url(http_url)

		# TODO: this should be get only
		subdomain, _ = Subdomain.objects.get_or_create(
			name=subdomain_name,
			scan_history=self.scan,
			target_domain=self.domain
		)

		# Get or create EndPoint object
		response = line.get('response')
		httpx_crawl = False if response else enable_http_crawl # avoid yet another httpx crawl
		endpoint, _ = save_endpoint(
			http_url,
			crawl=httpx_crawl,
			subdomain=subdomain,
			ctx=ctx)
		if endpoint:
			http_url = endpoint.http_url
			if not httpx_crawl:
				output = parse_curl_output(response)
				endpoint.http_status = output['http_status']
				endpoint.save()

		# Get or create Vulnerability object
		vuln, _ = save_vulnerability(
			target_domain=self.domain,
			http_url=http_url,
			scan_history=self.scan,
			subscan=self.subscan,
			subdomain=subdomain,
			**vuln_data)
		if not vuln:
			continue

		# Print vuln
		severity = line['info'].get('severity', 'unknown')
		logger.warning(str(vuln))

		# Send notification for all vulnerabilities except info
		url = vuln.http_url or vuln.subdomain
		send_vuln = (
			notif and
			notif.send_vuln_notif and
			vuln and
			severity in ['low', 'medium', 'high', 'critical'])
		if send_vuln:
			fields = {
				'Severity': f'**{severity.upper()}**',
				'URL': http_url,
				'Subdomain': subdomain_name,
				'Name': vuln.name,
				'Type': vuln.type,
				'Description': vuln.description,
				'Template': vuln.template_url,
				'Tags': vuln.get_tags_str(),
				'CVEs': vuln.get_cve_str(),
				'CWEs': vuln.get_cwe_str(),
				'References': vuln.get_refs_str()
			}
			severity_map = {
				'low': 'info',
				'medium': 'warning',
				'high': 'error',
				'critical': 'error'
			}
			self.notify(
				f'vulnerability_scan_#{vuln.id}',
				severity_map[severity],
				fields,
				add_meta_info=False)

		# Send report to hackerone
		hackerone_query = Hackerone.objects.all()
		send_report = (
			hackerone_query.exists() and
			severity not in ('info', 'low') and
			vuln.target_domain.h1_team_handle
		)
		if send_report:
			hackerone = hackerone_query.first()
			if hackerone.send_critical and severity == 'critical':
				send_hackerone_report.delay(vuln.id)
			elif hackerone.send_high and severity == 'high':
				send_hackerone_report.delay(vuln.id)
			elif hackerone.send_medium and severity == 'medium':
				send_hackerone_report.delay(vuln.id)

	# Write results to JSON file
	with open(self.output_path, 'w') as f:
		json.dump(results, f, indent=4)

	# Send finish notif
	if send_status:
		vulns = Vulnerability.objects.filter(scan_history__id=self.scan_id)
		info_count = vulns.filter(severity=0).count()
		low_count = vulns.filter(severity=1).count()
		medium_count = vulns.filter(severity=2).count()
		high_count = vulns.filter(severity=3).count()
		critical_count = vulns.filter(severity=4).count()
		unknown_count = vulns.filter(severity=-1).count()
		vulnerability_count = info_count + low_count + medium_count + high_count + critical_count + unknown_count
		fields = {
			'Total': vulnerability_count,
			'Critical': critical_count,
			'High': high_count,
			'Medium': medium_count,
			'Low': low_count,
			'Info': info_count,
			'Unknown': unknown_count
		}
		self.notify(fields=fields)

	# return results
	return None


@app.task(base=RengineTask, bind=True)
def dalfox_xss_scan(self, urls=[], ctx={}, description=None):
	"""XSS Scan using dalfox

	Args:
		urls (list, optional): If passed, filter on those URLs.
		description (str, optional): Task description shown in UI.
	"""
	vuln_config = self.yaml_configuration.get(VULNERABILITY_SCAN) or {}
	dalfox_config = vuln_config.get(DALFOX) or {}
	custom_header = dalfox_config.get(CUSTOM_HEADER) or self.yaml_configuration.get(CUSTOM_HEADER)
	proxy = get_random_proxy()
	is_waf_evasion = dalfox_config.get(WAF_EVASION, False)
	blind_xss_server = dalfox_config.get(BLIND_XSS_SERVER)
	user_agent = dalfox_config.get(USER_AGENT) or self.yaml_configuration.get(USER_AGENT)
	timeout = dalfox_config.get(TIMEOUT)
	delay = dalfox_config.get(DELAY)
	threads = dalfox_config.get(THREADS) or self.yaml_configuration.get(THREADS, DEFAULT_THREADS)
	input_path = f'{self.results_dir}/input_endpoints_dalfox_xss.txt'

	if urls:
		with open(input_path, 'w') as f:
			f.write('\n'.join(urls))
	else:
		get_http_urls(
			is_alive=False,
			ignore_files=False,
			write_filepath=input_path,
			ctx=ctx
		)

	notif = Notification.objects.first()
	send_status = notif.send_scan_status_notif if notif else False

	# command builder
	cmd = 'dalfox --silence --no-color --no-spinner'
	cmd += f' --only-poc r '
	cmd += f' --ignore-return 302,404,403'
	cmd += f' --skip-bav'
	cmd += f' file {input_path}'
	cmd += f' --proxy {proxy}' if proxy else ''
	cmd += f' --waf-evasion' if is_waf_evasion else ''
	cmd += f' -b {blind_xss_server}' if blind_xss_server else ''
	cmd += f' --delay {delay}' if delay else ''
	cmd += f' --timeout {timeout}' if timeout else ''
	cmd += f' --user-agent {user_agent}' if user_agent else ''
	cmd += f' --header {custom_header}' if custom_header else ''
	cmd += f' --worker {threads}' if threads else ''
	cmd += f' --format json'

	results = []
	for line in stream_command(
			cmd,
			history_file=self.history_file,
			scan_id=self.scan_id,
			activity_id=self.activity_id,
			trunc_char=','
		):
		if not isinstance(line, dict):
			continue

		results.append(line)

		vuln_data = parse_dalfox_result(line)

		http_url = sanitize_url(line.get('data'))
		subdomain_name = get_subdomain_from_url(http_url)

		# TODO: this should be get only
		subdomain, _ = Subdomain.objects.get_or_create(
			name=subdomain_name,
			scan_history=self.scan,
			target_domain=self.domain
		)
		endpoint, _ = save_endpoint(
			http_url,
			crawl=True,
			subdomain=subdomain,
			ctx=ctx
		)
		if endpoint:
			http_url = endpoint.http_url
			endpoint.save()

		vuln, _ = save_vulnerability(
			target_domain=self.domain,
			http_url=http_url,
			scan_history=self.scan,
			subscan=self.subscan,
			**vuln_data
		)

		if not vuln:
			continue
	return results


@app.task(base=RengineTask, bind=True)
def crlfuzz(self, urls=[], ctx={}, description=None):
	"""CRLF Fuzzing with CRLFuzz

	Args:
		urls (list, optional): If passed, filter on those URLs.
		description (str, optional): Task description shown in UI.
	"""
	vuln_config = self.yaml_configuration.get(VULNERABILITY_SCAN) or {}
	custom_header = vuln_config.get(CUSTOM_HEADER) or self.yaml_configuration.get(CUSTOM_HEADER)
	proxy = get_random_proxy()
	user_agent = vuln_config.get(USER_AGENT) or self.yaml_configuration.get(USER_AGENT)
	threads = vuln_config.get(THREADS) or self.yaml_configuration.get(THREADS, DEFAULT_THREADS)
	input_path = f'{self.results_dir}/input_endpoints_crlf.txt'
	output_path = f'{self.results_dir}/{self.filename}'

	if urls:
		with open(input_path, 'w') as f:
			f.write('\n'.join(urls))
	else:
		get_http_urls(
			is_alive=False,
			ignore_files=True,
			write_filepath=input_path,
			ctx=ctx
		)

	notif = Notification.objects.first()
	send_status = notif.send_scan_status_notif if notif else False

	# command builder
	cmd = 'crlfuzz -s'
	cmd += f' -l {input_path}'
	cmd += f' -x {proxy}' if proxy else ''
	cmd += f' --H {custom_header}' if custom_header else ''
	cmd += f' -o {output_path}'

	run_command(
		cmd,
		shell=False,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id
	)

	if not os.path.isfile(output_path):
		logger.info('No Results from CRLFuzz')
		return

	crlfs = []
	results = []
	with open(output_path, 'r') as file:
		crlfs = file.readlines()

	for crlf in crlfs:
		url = crlf.strip()

		vuln_data = parse_crlfuzz_result(url)

		http_url = sanitize_url(url)
		subdomain_name = get_subdomain_from_url(http_url)

		subdomain, _ = Subdomain.objects.get_or_create(
			name=subdomain_name,
			scan_history=self.scan,
			target_domain=self.domain
		)

		endpoint, _ = save_endpoint(
			http_url,
			crawl=True,
			subdomain=subdomain,
			ctx=ctx
		)
		if endpoint:
			http_url = endpoint.http_url
			endpoint.save()

		vuln, _ = save_vulnerability(
			target_domain=self.domain,
			http_url=http_url,
			scan_history=self.scan,
			subscan=self.subscan,
			**vuln_data
		)

		if not vuln:
			continue
	return results


@app.task(base=RengineTask, bind=True)
def http_crawl(
		self,
		urls=[],
		method=None,
		recrawl=False,
		ctx={},
		track=True,
		description=None,
		is_ran_from_subdomain_scan=False,
		should_remove_duplicate_endpoints=True,
		duplicate_removal_fields=[]):
	"""Use httpx to query HTTP URLs for important info like page titles, http
	status, etc...

	Args:
		urls (list, optional): A set of URLs to check. Overrides default
			behavior which queries all endpoints related to this scan.
		method (str): HTTP method to use (GET, HEAD, POST, PUT, DELETE).
		recrawl (bool, optional): If False, filter out URLs that have already
			been crawled.
		should_remove_duplicate_endpoints (bool): Whether to remove duplicate endpoints
		duplicate_removal_fields (list): List of Endpoint model fields to check for duplicates

	Returns:
		list: httpx results.
	"""
	logger.info('Initiating HTTP Crawl')
	if is_ran_from_subdomain_scan:
		logger.info('Running From Subdomain Scan...')
	cmd = '/go/bin/httpx'
	cfg = self.yaml_configuration.get(HTTP_CRAWL) or {}
	custom_header = cfg.get(CUSTOM_HEADER, '')
	threads = cfg.get(THREADS, DEFAULT_THREADS)
	follow_redirect = cfg.get(FOLLOW_REDIRECT, True)
	self.output_path = None
	input_path = f'{self.results_dir}/httpx_input.txt'
	history_file = f'{self.results_dir}/commands.txt'
	if urls: # direct passing URLs to check
		if self.url_filter:
			urls = [u for u in urls if self.url_filter in u]
		with open(input_path, 'w') as f:
			f.write('\n'.join(urls))
	else:
		urls = get_http_urls(
			is_uncrawled=not recrawl,
			write_filepath=input_path,
			ctx=ctx
		)
		# logger.debug(urls)

	# If no URLs found, skip it
	if not urls:
		return

	# Re-adjust thread number if few URLs to avoid spinning up a monster to
	# kill a fly.
	if len(urls) < threads:
		threads = len(urls)

	# Get random proxy
	proxy = get_random_proxy()

	# Run command
	cmd += f' -cl -ct -rt -location -td -websocket -cname -asn -cdn -probe -random-agent'
	cmd += f' -t {threads}' if threads > 0 else ''
	cmd += f' --http-proxy {proxy}' if proxy else ''
	cmd += f' -H "{custom_header}"' if custom_header else ''
	cmd += f' -json'
	cmd += f' -u {urls[0]}' if len(urls) == 1 else f' -l {input_path}'
	cmd += f' -x {method}' if method else ''
	cmd += f' -silent'
	if follow_redirect:
		cmd += ' -fr'
	results = []
	endpoint_ids = []
	for line in stream_command(
			cmd,
			history_file=history_file,
			scan_id=self.scan_id,
			activity_id=self.activity_id):

		if not line or not isinstance(line, dict):
			continue

		logger.debug(line)

		# No response from endpoint
		if line.get('failed', False):
			continue

		# Parse httpx output
		host = line.get('host', '')
		content_length = line.get('content_length', 0)
		http_status = line.get('status_code')
		http_url, is_redirect = extract_httpx_url(line)
		page_title = line.get('title')
		webserver = line.get('webserver')
		cdn = line.get('cdn', False)
		rt = line.get('time')
		techs = line.get('tech', [])
		cname = line.get('cname', '')
		content_type = line.get('content_type', '')
		response_time = -1
		if rt:
			response_time = float(''.join(ch for ch in rt if not ch.isalpha()))
			if rt[-2:] == 'ms':
				response_time = response_time / 1000

		# Create Subdomain object in DB
		subdomain_name = get_subdomain_from_url(http_url)
		subdomain, _ = save_subdomain(subdomain_name, ctx=ctx)

		# Save default HTTP URL to endpoint object in DB
		endpoint, created = save_endpoint(
			http_url,
			crawl=False,
			ctx=ctx,
			subdomain=subdomain,
			is_default=is_ran_from_subdomain_scan
		)
		if not endpoint:
			continue
		endpoint.http_status = http_status
		endpoint.page_title = page_title
		endpoint.content_length = content_length
		endpoint.webserver = webserver
		endpoint.response_time = response_time
		endpoint.content_type = content_type
		endpoint.save()
		endpoint_str = f'{http_url} [{http_status}] `{content_length}B` `{webserver}` `{rt}`'
		logger.warning(endpoint_str)
		if endpoint and endpoint.is_alive and endpoint.http_status != 403:
			self.notify(
				fields={'Alive endpoint': f'• {endpoint_str}'},
				add_meta_info=False)

		# Add endpoint to results
		line['_cmd'] = cmd
		line['final_url'] = http_url
		line['endpoint_id'] = endpoint.id
		line['endpoint_created'] = created
		line['is_redirect'] = is_redirect
		results.append(line)

		# Add technology objects to DB
		for technology in techs:
			tech, _ = Technology.objects.get_or_create(name=technology)
			endpoint.technologies.add(tech)
			if is_ran_from_subdomain_scan:
				subdomain.technologies.add(tech)
				subdomain.save()
			endpoint.save()
		techs_str = ', '.join([f'`{tech}`' for tech in techs])
		self.notify(
			fields={'Technologies': techs_str},
			add_meta_info=False)

		# Add IP objects for 'a' records to DB
		a_records = line.get('a', [])
		for ip_address in a_records:
			ip, created = save_ip_address(
				ip_address,
				subdomain,
				subscan=self.subscan,
				cdn=cdn)
		ips_str = '• ' + '\n• '.join([f'`{ip}`' for ip in a_records])
		self.notify(
			fields={'IPs': ips_str},
			add_meta_info=False)

		# Add IP object for host in DB
		if host:
			ip, created = save_ip_address(
				host,
				subdomain,
				subscan=self.subscan,
				cdn=cdn)
			self.notify(
				fields={'IPs': f'• `{ip.address}`'},
				add_meta_info=False)

		# Save subdomain and endpoint
		if is_ran_from_subdomain_scan:
			# save subdomain stuffs
			subdomain.http_url = http_url
			subdomain.http_status = http_status
			subdomain.page_title = page_title
			subdomain.content_length = content_length
			subdomain.webserver = webserver
			subdomain.response_time = response_time
			subdomain.content_type = content_type
			subdomain.cname = ','.join(cname)
			subdomain.is_cdn = cdn
			if cdn:
				subdomain.cdn_name = line.get('cdn_name')
			subdomain.save()
		endpoint.save()
		endpoint_ids.append(endpoint.id)

	if should_remove_duplicate_endpoints:
		# Remove 'fake' alive endpoints that are just redirects to the same page
		remove_duplicate_endpoints(
			self.scan_id,
			self.domain_id,
			self.subdomain_id,
			filter_ids=endpoint_ids
		)

	# Remove input file
	run_command(
		f'rm {input_path}',
		shell=True,
		history_file=self.history_file,
		scan_id=self.scan_id,
		activity_id=self.activity_id)

	return results


#---------------------#
# Notifications tasks #
#---------------------#

@app.task
def send_notif(
		message,
		scan_history_id=None,
		subscan_id=None,
		**options):
	if not 'title' in options:
		message = enrich_notification(message, scan_history_id, subscan_id)
	send_discord_message(message, **options)
	send_slack_message(message)
	send_telegram_message(message)


@app.task
def send_scan_notif(
		scan_history_id,
		subscan_id=None,
		engine_id=None,
		status='RUNNING'):
	"""Send scan status notification. Works for scan or a subscan if subscan_id
	is passed.

	Args:
		scan_history_id (int, optional): ScanHistory id.
		subscan_id (int, optional): SuScan id.
		engine_id (int, optional): EngineType id.
	"""

	# Skip send if notification settings are not configured
	notif = Notification.objects.first()
	if not (notif and notif.send_scan_status_notif):
		return

	# Get domain, engine, scan_history objects
	engine = EngineType.objects.filter(pk=engine_id).first()
	scan = ScanHistory.objects.filter(pk=scan_history_id).first()
	subscan = SubScan.objects.filter(pk=subscan_id).first()
	tasks = ScanActivity.objects.filter(scan_of=scan) if scan else 0

	# Build notif options
	url = get_scan_url(scan_history_id, subscan_id)
	title = get_scan_title(scan_history_id, subscan_id)
	fields = get_scan_fields(engine, scan, subscan, status, tasks)
	severity = None
	msg = f'{title} {status}\n'
	msg += '\n🡆 '.join(f'**{k}:** {v}' for k, v in fields.items())
	if status:
		severity = STATUS_TO_SEVERITIES.get(status)
	opts = {
		'title': title,
		'url': url,
		'fields': fields,
		'severity': severity
	}
	logger.warning(f'Sending notification "{title}" [{severity}]')

	# Send notification
	send_notif(
		msg,
		scan_history_id,
		subscan_id,
		**opts)


@app.task
def send_task_notif(
		task_name,
		status=None,
		result=None,
		output_path=None,
		traceback=None,
		scan_history_id=None,
		engine_id=None,
		subscan_id=None,
		severity=None,
		add_meta_info=True,
		update_fields={}):
	"""Send task status notification.

	Args:
		task_name (str): Task name.
		status (str, optional): Task status.
		result (str, optional): Task result.
		output_path (str, optional): Task output path.
		traceback (str, optional): Task traceback.
		scan_history_id (int, optional): ScanHistory id.
		subscan_id (int, optional): SuScan id.
		engine_id (int, optional): EngineType id.
		severity (str, optional): Severity (will be mapped to notif colors)
		add_meta_info (bool, optional): Wheter to add scan / subscan info to notif.
		update_fields (dict, optional): Fields key / value to update.
	"""

	# Skip send if notification settings are not configured
	notif = Notification.objects.first()
	if not (notif and notif.send_scan_status_notif):
		return

	# Build fields
	url = None
	fields = {}
	if add_meta_info:
		engine = EngineType.objects.filter(pk=engine_id).first()
		scan = ScanHistory.objects.filter(pk=scan_history_id).first()
		subscan = SubScan.objects.filter(pk=subscan_id).first()
		url = get_scan_url(scan_history_id)
		if status:
			fields['Status'] = f'**{status}**'
		if engine:
			fields['Engine'] = engine.engine_name
		if scan:
			fields['Scan ID'] = f'[#{scan.id}]({url})'
		if subscan:
			url = get_scan_url(scan_history_id, subscan_id)
			fields['Subscan ID'] = f'[#{subscan.id}]({url})'
	title = get_task_title(task_name, scan_history_id, subscan_id)
	if status:
		severity = STATUS_TO_SEVERITIES.get(status)

	msg = f'{title} {status}\n'
	msg += '\n🡆 '.join(f'**{k}:** {v}' for k, v in fields.items())

	# Add fields to update
	for k, v in update_fields.items():
		fields[k] = v

	# Add traceback to notif
	if traceback and notif.send_scan_tracebacks:
		fields['Traceback'] = f'```\n{traceback}\n```'

	# Add files to notif
	files = []
	attach_file = (
		notif.send_scan_output_file and
		output_path and
		result and
		not traceback
	)
	if attach_file:
		output_title = output_path.split('/')[-1]
		files = [(output_path, output_title)]

	# Send notif
	opts = {
		'title': title,
		'url': url,
		'files': files,
		'severity': severity,
		'fields': fields,
		'fields_append': update_fields.keys()
	}
	send_notif(
		msg,
		scan_history_id=scan_history_id,
		subscan_id=subscan_id,
		**opts)


@app.task
def send_file_to_discord(file_path, title=None):
	notif = Notification.objects.first()
	do_send = notif and notif.send_to_discord and notif.discord_hook_url
	if not do_send:
		return False

	webhook = DiscordWebhook(
		url=notif.discord_hook_url,
		rate_limit_retry=True,
		username=title or "reNgine Discord Plugin"
	)
	with open(file_path, "rb") as f:
		head, tail = os.path.split(file_path)
		webhook.add_file(file=f.read(), filename=tail)
	webhook.execute()


@app.task
def send_hackerone_report(vulnerability_id):
	"""Send HackerOne vulnerability report.

	Args:
		vulnerability_id (int): Vulnerability id.

	Returns:
		int: HTTP response status code.
	"""
	vulnerability = Vulnerability.objects.get(id=vulnerability_id)
	severities = {v: k for k,v in NUCLEI_SEVERITY_MAP.items()}
	headers = {
		'Content-Type': 'application/json',
		'Accept': 'application/json'
	}

	# can only send vulnerability report if team_handle exists
	if len(vulnerability.target_domain.h1_team_handle) !=0:
		hackerone_query = Hackerone.objects.all()
		if hackerone_query.exists():
			hackerone = Hackerone.objects.first()
			severity_value = severities[vulnerability.severity]
			tpl = hackerone.report_template

			# Replace syntax of report template with actual content
			tpl = tpl.replace('{vulnerability_name}', vulnerability.name)
			tpl = tpl.replace('{vulnerable_url}', vulnerability.http_url)
			tpl = tpl.replace('{vulnerability_severity}', severity_value)
			tpl = tpl.replace('{vulnerability_description}', vulnerability.description if vulnerability.description else '')
			tpl = tpl.replace('{vulnerability_extracted_results}', vulnerability.extracted_results if vulnerability.extracted_results else '')
			tpl = tpl.replace('{vulnerability_reference}', vulnerability.reference if vulnerability.reference else '')

			data = {
			  "data": {
				"type": "report",
				"attributes": {
				  "team_handle": vulnerability.target_domain.h1_team_handle,
				  "title": '{} found in {}'.format(vulnerability.name, vulnerability.http_url),
				  "vulnerability_information": tpl,
				  "severity_rating": severity_value,
				  "impact": "More information about the impact and vulnerability can be found here: \n" + vulnerability.reference if vulnerability.reference else "NA",
				}
			  }
			}

			r = requests.post(
			  'https://api.hackerone.com/v1/hackers/reports',
			  auth=(hackerone.username, hackerone.api_key),
			  json=data,
			  headers=headers
			)
			response = r.json()
			status_code = r.status_code
			if status_code == 201:
				vulnerability.hackerone_report_id = response['data']["id"]
				vulnerability.open_status = False
				vulnerability.save()
			return status_code

	else:
		logger.error('No team handle found.')
		status_code = 111
		return status_code


#-------------#
# Utils tasks #
#-------------#


@app.task
def parse_nmap_results(xml_file, output_file=None):
	"""Parse results from nmap output file.

	Args:
		xml_file (str): nmap XML report file path.

	Returns:
		list: List of vulnerabilities found from nmap results.
	"""
	with open(xml_file, encoding='utf8') as f:
		content = f.read()
		try:
			nmap_results = xmltodict.parse(content) # parse XML to dict
		except Exception as e:
			logger.exception(e)
			logger.error(f'Cannot parse {xml_file} to valid JSON. Skipping.')
			return []

	# Write JSON to output file
	if output_file:
		with open(output_file, 'w') as f:
			json.dump(nmap_results, f, indent=4)
	logger.warning(json.dumps(nmap_results, indent=4))
	hosts = (
		nmap_results
		.get('nmaprun', {})
		.get('host', {})
	)
	all_vulns = []
	if isinstance(hosts, dict):
		hosts = [hosts]

	for host in hosts:
		# Grab hostname / IP from output
		hostnames = host.get('hostnames', {})
		if hostnames:
			hostname = hostnames.get('hostname', {}).get('@name')
		else:
			hostname = host.get('address')['@addr']

		# Grab ports from output
		ports = host.get('ports', {}).get('port', [])
		if isinstance(ports, dict):
			ports = [ports]

		for port in ports:
			url_vulns = []
			port_number = port['@portid']
			url = sanitize_url(f'{hostname}:{port_number}')
			logger.info(f'Parsing nmap results for {hostname}:{port_number} ...')
			if not port_number or not port_number.isdigit():
				continue
			port_protocol = port['@protocol']
			scripts = port.get('script', [])
			if isinstance(scripts, dict):
				scripts = [scripts]

			for script in scripts:
				script_id = script['@id']
				script_output = script['@output']
				script_output_table = script.get('table', [])
				logger.debug(f'Ran nmap script "{script_id}" on {port_number}/{port_protocol}:\n{script_output}\n')
				if script_id == 'vulscan':
					vulns = parse_nmap_vulscan_output(script_output)
					url_vulns.extend(vulns)
				elif script_id == 'vulners':
					vulns = parse_nmap_vulners_output(script_output)
					url_vulns.extend(vulns)
				# elif script_id == 'http-server-header':
				# 	TODO: nmap can help find technologies as well using the http-server-header script
				# 	regex = r'(\w+)/([\d.]+)\s?(?:\((\w+)\))?'
				# 	tech_name, tech_version, tech_os = re.match(regex, test_string).groups()
				# 	Technology.objects.get_or_create(...)
				# elif script_id == 'http_csrf':
				# 	vulns = parse_nmap_http_csrf_output(script_output)
				# 	url_vulns.extend(vulns)
				else:
					logger.warning(f'Script output parsing for script "{script_id}" is not supported yet.')

			# Add URL to vuln
			for vuln in url_vulns:
				# TODO: This should extend to any URL, not just HTTP
				vuln['http_url'] = url
				if 'http_path' in vuln:
					vuln['http_url'] += vuln['http_path']
				all_vulns.append(vuln)

	return all_vulns


def parse_nmap_http_csrf_output(script_output):
	pass


def parse_nmap_vulscan_output(script_output):
	"""Parse nmap vulscan script output.

	Args:
		script_output (str): Vulscan script output.

	Returns:
		list: List of Vulnerability dicts.
	"""
	data = {}
	vulns = []
	provider_name = ''

	# Sort all vulns found by provider so that we can match each provider with
	# a function that pulls from its API to get more info about the
	# vulnerability.
	for line in script_output.splitlines():
		if not line:
			continue
		if not line.startswith('['): # provider line
			provider_name, provider_url = tuple(line.split(' - '))
			data[provider_name] = {'url': provider_url.rstrip(':'), 'entries': []}
			continue
		reg = r'\[(.*)\] (.*)'
		matches = re.match(reg, line)
		id, title = matches.groups()
		entry = {'id': id, 'title': title}
		data[provider_name]['entries'].append(entry)

	logger.warning('Vulscan parsed output:')
	logger.warning(pprint.pformat(data))

	for provider_name in data:
		if provider_name == 'Exploit-DB':
			logger.error(f'Provider {provider_name} is not supported YET.')
			pass
		elif provider_name == 'IBM X-Force':
			logger.error(f'Provider {provider_name} is not supported YET.')
			pass
		elif provider_name == 'MITRE CVE':
			logger.error(f'Provider {provider_name} is not supported YET.')
			for entry in data[provider_name]['entries']:
				cve_id = entry['id']
				vuln = cve_to_vuln(cve_id)
				vulns.append(vuln)
		elif provider_name == 'OSVDB':
			logger.error(f'Provider {provider_name} is not supported YET.')
			pass
		elif provider_name == 'OpenVAS (Nessus)':
			logger.error(f'Provider {provider_name} is not supported YET.')
			pass
		elif provider_name == 'SecurityFocus':
			logger.error(f'Provider {provider_name} is not supported YET.')
			pass
		elif provider_name == 'VulDB':
			logger.error(f'Provider {provider_name} is not supported YET.')
			pass
		else:
			logger.error(f'Provider {provider_name} is not supported.')
	return vulns


def parse_nmap_vulners_output(script_output, url=''):
	"""Parse nmap vulners script output.

	TODO: Rework this as it's currently matching all CVEs no matter the
	confidence.

	Args:
		script_output (str): Script output.

	Returns:
		list: List of found vulnerabilities.
	"""
	vulns = []
	# Check for CVE in script output
	CVE_REGEX = re.compile(r'.*(CVE-\d\d\d\d-\d+).*')
	matches = CVE_REGEX.findall(script_output)
	matches = list(dict.fromkeys(matches))
	for cve_id in matches: # get CVE info
		vuln = cve_to_vuln(cve_id, vuln_type='nmap-vulners-nse')
		if vuln:
			vulns.append(vuln)
	return vulns


def cve_to_vuln(cve_id, vuln_type=''):
	"""Search for a CVE using CVESearch and return Vulnerability data.

	Args:
		cve_id (str): CVE ID in the form CVE-*

	Returns:
		dict: Vulnerability dict.
	"""
	cve_info = CVESearch('https://cve.circl.lu').id(cve_id)
	if not cve_info:
		logger.error(f'Could not fetch CVE info for cve {cve_id}. Skipping.')
		return None
	vuln_cve_id = cve_info['id']
	vuln_name = vuln_cve_id
	vuln_description = cve_info.get('summary', 'none').replace(vuln_cve_id, '').strip()
	try:
		vuln_cvss = float(cve_info.get('cvss', -1))
	except (ValueError, TypeError):
		vuln_cvss = -1
	vuln_cwe_id = cve_info.get('cwe', '')
	exploit_ids = cve_info.get('refmap', {}).get('exploit-db', [])
	osvdb_ids = cve_info.get('refmap', {}).get('osvdb', [])
	references = cve_info.get('references', [])
	capec_objects = cve_info.get('capec', [])

	# Parse ovals for a better vuln name / type
	ovals = cve_info.get('oval', [])
	if ovals:
		vuln_name = ovals[0]['title']
		vuln_type = ovals[0]['family']

	# Set vulnerability severity based on CVSS score
	vuln_severity = 'info'
	if vuln_cvss < 4:
		vuln_severity = 'low'
	elif vuln_cvss < 7:
		vuln_severity = 'medium'
	elif vuln_cvss < 9:
		vuln_severity = 'high'
	else:
		vuln_severity = 'critical'

	# Build console warning message
	msg = f'{vuln_name} | {vuln_severity.upper()} | {vuln_cve_id} | {vuln_cwe_id} | {vuln_cvss}'
	for id in osvdb_ids:
		msg += f'\n\tOSVDB: {id}'
	for exploit_id in exploit_ids:
		msg += f'\n\tEXPLOITDB: {exploit_id}'
	logger.warning(msg)
	vuln = {
		'name': vuln_name,
		'type': vuln_type,
		'severity': NUCLEI_SEVERITY_MAP[vuln_severity],
		'description': vuln_description,
		'cvss_score': vuln_cvss,
		'references': references,
		'cve_ids': [vuln_cve_id],
		'cwe_ids': [vuln_cwe_id]
	}
	return vuln


def parse_nuclei_result(line):
	"""Parse results from nuclei JSON output.

	Args:
		line (dict): Nuclei JSON line output.

	Returns:
		dict: Vulnerability data.
	"""
	return {
		'name': line['info'].get('name', ''),
		'type': line['type'],
		'severity': NUCLEI_SEVERITY_MAP[line['info'].get('severity', 'unknown')],
		'template': line['template'],
		'template_url': line['template-url'],
		'template_id': line['template-id'],
		'description': line['info'].get('description', ''),
		'matcher_name': line.get('matcher-name', ''),
		'curl_command': line.get('curl-command'),
		'request': line.get('request'),
		'response': line.get('response'),
		'extracted_results': line.get('extracted-results', []),
		'cvss_metrics': line['info'].get('classification', {}).get('cvss-metrics', ''),
		'cvss_score': line['info'].get('classification', {}).get('cvss-score'),
		'cve_ids': line['info'].get('classification', {}).get('cve_id', []) or [],
		'cwe_ids': line['info'].get('classification', {}).get('cwe_id', []) or [],
		'references': line['info'].get('reference', []) or [],
		'tags': line['info'].get('tags', []),
		'source': 'nuclei',
	}


def parse_dalfox_result(line):
	"""Parse results from nuclei JSON output.

	Args:
		line (dict): Nuclei JSON line output.

	Returns:
		dict: Vulnerability data.
	"""

	description = ''
	description += f" Evidence: {line.get('evidence')} <br>" if line.get('evidence') else ''
	description += f" Message: {line.get('message')} <br>" if line.get('message') else ''
	description += f" Payload: {line.get('message_str')} <br>" if line.get('message_str') else ''
	description += f" Vulnerable Parameter: {line.get('param')} <br>" if line.get('param') else ''

	return {
		'name': 'XSS (Cross Site Scripting)',
		'type': 'XSS',
		'severity': DALFOX_SEVERITY_MAP[line.get('severity', 'unknown')],
		'description': description,
		'source': 'Dalfox',
		'cwe_ids': [line.get('cwe')]
	}


def parse_crlfuzz_result(url):
	"""Parse CRLF results

	Args:
		url (str): CRLF Vulnerable URL

	Returns:
		dict: Vulnerability data.
	"""

	return {
		'name': 'CRLF (HTTP Response Splitting)',
		'type': 'CRLF',
		'severity': 2,
		'description': '',
		'source': 'CRLFUZZ',
	}


@app.task
def geo_localize(host, ip_id=None):
	"""Uses geoiplookup to find location associated with host.

	Args:
		host (str): Hostname.
		ip_id (int): IpAddress object id.

	Returns:
		startScan.models.CountryISO: CountryISO object from DB or None.
	"""
	if validators.ipv6(host):
		logger.info(f'Ipv6 "{host}" is not supported by geoiplookup. Skipping.')
		return None
	cmd = f'geoiplookup {host}'
	_, out = run_command(cmd)
	if 'IP Address not found' not in out and "can't resolve hostname" not in out:
		country_iso = out.split(':')[1].strip().split(',')[0]
		country_name = out.split(':')[1].strip().split(',')[1].strip()
		geo_object, _ = CountryISO.objects.get_or_create(
			iso=country_iso,
			name=country_name
		)
		geo_json = {
			'iso': country_iso,
			'name': country_name
		}
		if ip_id:
			ip = IpAddress.objects.get(pk=ip_id)
			ip.geo_iso = geo_object
			ip.save()
		return geo_json
	logger.info(f'Geo IP lookup failed for host "{host}"')
	return None

@app.task
def query_whois(ip_domain, force_reload_whois=False):
	"""Query WHOIS information for an IP or a domain name.

	Args:
		ip_domain (str): IP address or domain name.
		save_domain (bool): Whether to save domain or not, default False
	Returns:
		dict: WHOIS information.
	"""
	if not force_reload_whois and Domain.objects.filter(name=ip_domain).exists() and Domain.objects.get(name=ip_domain).domain_info:
		domain = Domain.objects.get(name=ip_domain)
		if not domain.insert_date:
			domain.insert_date = timezone.now()
			domain.save()
		domain_info_db = domain.domain_info
		domain_info = DottedDict(
			dnssec=domain_info_db.dnssec,
			created=domain_info_db.created,
			updated=domain_info_db.updated,
			expires=domain_info_db.expires,
			geolocation_iso=domain_info_db.geolocation_iso,
			status=[status['name'] for status in DomainWhoisStatusSerializer(domain_info_db.status, many=True).data],
			whois_server=domain_info_db.whois_server,
			ns_records=[ns['name'] for ns in NameServersSerializer(domain_info_db.name_servers, many=True).data],
			registrar_name=domain_info_db.registrar.name,
			registrar_phone=domain_info_db.registrar.phone,
			registrar_email=domain_info_db.registrar.email,
			registrar_url=domain_info_db.registrar.url,
			registrant_name=domain_info_db.registrant.name,
			registrant_id=domain_info_db.registrant.id_str,
			registrant_organization=domain_info_db.registrant.organization,
			registrant_city=domain_info_db.registrant.city,
			registrant_state=domain_info_db.registrant.state,
			registrant_zip_code=domain_info_db.registrant.zip_code,
			registrant_country=domain_info_db.registrant.country,
			registrant_phone=domain_info_db.registrant.phone,
			registrant_fax=domain_info_db.registrant.fax,
			registrant_email=domain_info_db.registrant.email,
			registrant_address=domain_info_db.registrant.address,
			admin_name=domain_info_db.admin.name,
			admin_id=domain_info_db.admin.id_str,
			admin_organization=domain_info_db.admin.organization,
			admin_city=domain_info_db.admin.city,
			admin_state=domain_info_db.admin.state,
			admin_zip_code=domain_info_db.admin.zip_code,
			admin_country=domain_info_db.admin.country,
			admin_phone=domain_info_db.admin.phone,
			admin_fax=domain_info_db.admin.fax,
			admin_email=domain_info_db.admin.email,
			admin_address=domain_info_db.admin.address,
			tech_name=domain_info_db.tech.name,
			tech_id=domain_info_db.tech.id_str,
			tech_organization=domain_info_db.tech.organization,
			tech_city=domain_info_db.tech.city,
			tech_state=domain_info_db.tech.state,
			tech_zip_code=domain_info_db.tech.zip_code,
			tech_country=domain_info_db.tech.country,
			tech_phone=domain_info_db.tech.phone,
			tech_fax=domain_info_db.tech.fax,
			tech_email=domain_info_db.tech.email,
			tech_address=domain_info_db.tech.address,
			related_tlds=[domain['name'] for domain in RelatedDomainSerializer(domain_info_db.related_tlds, many=True).data],
			related_domains=[domain['name'] for domain in RelatedDomainSerializer(domain_info_db.related_domains, many=True).data],
			historical_ips=[ip for ip in HistoricalIPSerializer(domain_info_db.historical_ips, many=True).data],
		)
		if domain_info_db.dns_records:
			a_records = []
			txt_records = []
			mx_records = []
			dns_records = [{'name': dns['name'], 'type': dns['type']} for dns in DomainDNSRecordSerializer(domain_info_db.dns_records, many=True).data]
			for dns in dns_records:
				if dns['type'] == 'a':
					a_records.append(dns['name'])
				elif dns['type'] == 'txt':
					txt_records.append(dns['name'])
				elif dns['type'] == 'mx':
					mx_records.append(dns['name'])
			domain_info.a_records = a_records
			domain_info.txt_records = txt_records
			domain_info.mx_records = mx_records
	else:
		logger.info(f'Domain info for "{ip_domain}" not found in DB, querying whois')
		domain_info = DottedDict()
		# find domain historical ip
		try:
			historical_ips = get_domain_historical_ip_address(ip_domain)
			domain_info.historical_ips = historical_ips
		except Exception as e:
			logger.error(f'HistoricalIP for {ip_domain} not found!\nError: {str(e)}')
			historical_ips = []
		# find associated domains using ip_domain
		try:
			related_domains = reverse_whois(ip_domain.split('.')[0])
		except Exception as e:
			logger.error(f'Associated domain not found for {ip_domain}\nError: {str(e)}')
			similar_domains = []
		# find related tlds using TLSx
		try:
			related_tlds = []
			output_path = '/tmp/ip_domain_tlsx.txt'
			tlsx_command = f'tlsx -san -cn -silent -ro -host {ip_domain} -o {output_path}'
			run_command(
				tlsx_command,
				shell=True,
			)
			tlsx_output = []
			with open(output_path) as f:
				tlsx_output = f.readlines()

			tldextract_target = tldextract.extract(ip_domain)
			for doms in tlsx_output:
				doms = doms.strip()
				tldextract_res = tldextract.extract(doms)
				if ip_domain != doms and tldextract_res.domain == tldextract_target.domain and tldextract_res.subdomain == '':
					related_tlds.append(doms)

			related_tlds = list(set(related_tlds))
			domain_info.related_tlds = related_tlds
		except Exception as e:
			logger.error(f'Associated domain not found for {ip_domain}\nError: {str(e)}')
			similar_domains = []

		related_domains_list = []
		if Domain.objects.filter(name=ip_domain).exists():
			domain = Domain.objects.get(name=ip_domain)
			db_domain_info = domain.domain_info if domain.domain_info else DomainInfo()
			db_domain_info.save()
			for _domain in related_domains:
				domain_related = RelatedDomain.objects.get_or_create(
					name=_domain['name'],
				)[0]
				db_domain_info.related_domains.add(domain_related)
				related_domains_list.append(_domain['name'])

			for _domain in related_tlds:
				domain_related = RelatedDomain.objects.get_or_create(
					name=_domain,
				)[0]
				db_domain_info.related_tlds.add(domain_related)

			for _ip in historical_ips:
				historical_ip = HistoricalIP.objects.get_or_create(
					ip=_ip['ip'],
					owner=_ip['owner'],
					location=_ip['location'],
					last_seen=_ip['last_seen'],
				)[0]
				db_domain_info.historical_ips.add(historical_ip)
			domain.domain_info = db_domain_info
			domain.save()

		command = f'netlas host {ip_domain} -f json'
		result = subprocess.check_output(command.split()).decode('utf-8')
		if 'Failed to parse response data' in result:
			# do fallback
			return {
				'status': False,
				'ip_domain': ip_domain,
				'result': "Netlas limit exceeded.",
				'message': 'Netlas limit exceeded.'
			}
		try:
			result = json.loads(result)
			logger.info(result)
			whois = result.get('whois') if result.get('whois') else {}

			domain_info.created = whois.get('created_date')
			domain_info.expires = whois.get('expiration_date')
			domain_info.updated = whois.get('updated_date')
			domain_info.whois_server = whois.get('whois_server')


			if 'registrant' in whois:
				registrant = whois.get('registrant')
				domain_info.registrant_name = registrant.get('name')
				domain_info.registrant_country = registrant.get('country')
				domain_info.registrant_id = registrant.get('id')
				domain_info.registrant_state = registrant.get('province')
				domain_info.registrant_city = registrant.get('city')
				domain_info.registrant_phone = registrant.get('phone')
				domain_info.registrant_address = registrant.get('street')
				domain_info.registrant_organization = registrant.get('organization')
				domain_info.registrant_fax = registrant.get('fax')
				domain_info.registrant_zip_code = registrant.get('postal_code')
				email_search = EMAIL_REGEX.search(str(registrant.get('email')))
				field_content = email_search.group(0) if email_search else None
				domain_info.registrant_email = field_content

			if 'administrative' in whois:
				administrative = whois.get('administrative')
				domain_info.admin_name = administrative.get('name')
				domain_info.admin_country = administrative.get('country')
				domain_info.admin_id = administrative.get('id')
				domain_info.admin_state = administrative.get('province')
				domain_info.admin_city = administrative.get('city')
				domain_info.admin_phone = administrative.get('phone')
				domain_info.admin_address = administrative.get('street')
				domain_info.admin_organization = administrative.get('organization')
				domain_info.admin_fax = administrative.get('fax')
				domain_info.admin_zip_code = administrative.get('postal_code')
				mail_search = EMAIL_REGEX.search(str(administrative.get('email')))
				field_content = email_search.group(0) if email_search else None
				domain_info.admin_email = field_content

			if 'technical' in whois:
				technical = whois.get('technical')
				domain_info.tech_name = technical.get('name')
				domain_info.tech_country = technical.get('country')
				domain_info.tech_state = technical.get('province')
				domain_info.tech_id = technical.get('id')
				domain_info.tech_city = technical.get('city')
				domain_info.tech_phone = technical.get('phone')
				domain_info.tech_address = technical.get('street')
				domain_info.tech_organization = technical.get('organization')
				domain_info.tech_fax = technical.get('fax')
				domain_info.tech_zip_code = technical.get('postal_code')
				mail_search = EMAIL_REGEX.search(str(technical.get('email')))
				field_content = email_search.group(0) if email_search else None
				domain_info.tech_email = field_content

			if 'dns' in result:
				dns = result.get('dns')
				domain_info.mx_records = dns.get('mx')
				domain_info.txt_records = dns.get('txt')
				domain_info.a_records = dns.get('a')

			domain_info.ns_records = whois.get('name_servers')
			domain_info.dnssec = True if whois.get('dnssec') else False
			domain_info.status = whois.get('status')

			if 'registrar' in whois:
				registrar = whois.get('registrar')
				domain_info.registrar_name = registrar.get('name')
				domain_info.registrar_email = registrar.get('email')
				domain_info.registrar_phone = registrar.get('phone')
				domain_info.registrar_url = registrar.get('url')

			# find associated domains if registrant email is found
			related_domains = reverse_whois(domain_info.get('registrant_email')) if domain_info.get('registrant_email') else []
			for _domain in related_domains:
				related_domains_list.append(_domain['name'])

			# remove duplicate domains from related domains list
			related_domains_list = list(set(related_domains_list))
			domain_info.related_domains = related_domains_list

			# save to db if domain exists
			if Domain.objects.filter(name=ip_domain).exists():
				domain = Domain.objects.get(name=ip_domain)
				db_domain_info = domain.domain_info if domain.domain_info else DomainInfo()
				db_domain_info.save()
				for _domain in related_domains:
					domain_rel = RelatedDomain.objects.get_or_create(
						name=_domain['name'],
					)[0]
					db_domain_info.related_domains.add(domain_rel)

				db_domain_info.dnssec = domain_info.get('dnssec')
				#dates
				db_domain_info.created = domain_info.get('created')
				db_domain_info.updated = domain_info.get('updated')
				db_domain_info.expires = domain_info.get('expires')
				#registrar
				db_domain_info.registrar = Registrar.objects.get_or_create(
					name=domain_info.get('registrar_name'),
					email=domain_info.get('registrar_email'),
					phone=domain_info.get('registrar_phone'),
					url=domain_info.get('registrar_url'),
				)[0]
				db_domain_info.registrant = DomainRegistration.objects.get_or_create(
					name=domain_info.get('registrant_name'),
					organization=domain_info.get('registrant_organization'),
					address=domain_info.get('registrant_address'),
					city=domain_info.get('registrant_city'),
					state=domain_info.get('registrant_state'),
					zip_code=domain_info.get('registrant_zip_code'),
					country=domain_info.get('registrant_country'),
					email=domain_info.get('registrant_email'),
					phone=domain_info.get('registrant_phone'),
					fax=domain_info.get('registrant_fax'),
					id_str=domain_info.get('registrant_id'),
				)[0]
				db_domain_info.admin = DomainRegistration.objects.get_or_create(
					name=domain_info.get('admin_name'),
					organization=domain_info.get('admin_organization'),
					address=domain_info.get('admin_address'),
					city=domain_info.get('admin_city'),
					state=domain_info.get('admin_state'),
					zip_code=domain_info.get('admin_zip_code'),
					country=domain_info.get('admin_country'),
					email=domain_info.get('admin_email'),
					phone=domain_info.get('admin_phone'),
					fax=domain_info.get('admin_fax'),
					id_str=domain_info.get('admin_id'),
				)[0]
				db_domain_info.tech = DomainRegistration.objects.get_or_create(
					name=domain_info.get('tech_name'),
					organization=domain_info.get('tech_organization'),
					address=domain_info.get('tech_address'),
					city=domain_info.get('tech_city'),
					state=domain_info.get('tech_state'),
					zip_code=domain_info.get('tech_zip_code'),
					country=domain_info.get('tech_country'),
					email=domain_info.get('tech_email'),
					phone=domain_info.get('tech_phone'),
					fax=domain_info.get('tech_fax'),
					id_str=domain_info.get('tech_id'),
				)[0]
				for status in domain_info.get('status') or []:
					_status = WhoisStatus.objects.get_or_create(
						name=status
					)[0]
					_status.save()
					db_domain_info.status.add(_status)

				for ns in domain_info.get('ns_records') or []:
					_ns = NameServer.objects.get_or_create(
						name=ns
					)[0]
					_ns.save()
					db_domain_info.name_servers.add(_ns)

				for a in domain_info.get('a_records') or []:
					_a = DNSRecord.objects.get_or_create(
						name=a,
						type='a'
					)[0]
					_a.save()
					db_domain_info.dns_records.add(_a)
				for mx in domain_info.get('mx_records') or []:
					_mx = DNSRecord.objects.get_or_create(
						name=mx,
						type='mx'
					)[0]
					_mx.save()
					db_domain_info.dns_records.add(_mx)
				for txt in domain_info.get('txt_records') or []:
					_txt = DNSRecord.objects.get_or_create(
						name=txt,
						type='txt'
					)[0]
					_txt.save()
					db_domain_info.dns_records.add(_txt)

				db_domain_info.geolocation_iso = domain_info.get('registrant_country')
				db_domain_info.whois_server = domain_info.get('whois_server')
				db_domain_info.save()
				domain.domain_info = db_domain_info
				domain.save()

		except Exception as e:
			return {
				'status': False,
				'ip_domain': ip_domain,
				'result': "unable to fetch records from WHOIS database.",
				'message': str(e)
			}

	return {
		'status': True,
		'ip_domain': ip_domain,
		'dnssec': domain_info.get('dnssec'),
		'created': domain_info.get('created'),
		'updated': domain_info.get('updated'),
		'expires': domain_info.get('expires'),
		'geolocation_iso': domain_info.get('registrant_country'),
		'domain_statuses': domain_info.get('status'),
		'whois_server': domain_info.get('whois_server'),
		'dns': {
			'a': domain_info.get('a_records'),
			'mx': domain_info.get('mx_records'),
			'txt': domain_info.get('txt_records'),
		},
		'registrar': {
			'name': domain_info.get('registrar_name'),
			'phone': domain_info.get('registrar_phone'),
			'email': domain_info.get('registrar_email'),
			'url': domain_info.get('registrar_url'),
		},
		'registrant': {
			'name': domain_info.get('registrant_name'),
			'id': domain_info.get('registrant_id'),
			'organization': domain_info.get('registrant_organization'),
			'address': domain_info.get('registrant_address'),
			'city': domain_info.get('registrant_city'),
			'state': domain_info.get('registrant_state'),
			'zipcode': domain_info.get('registrant_zip_code'),
			'country': domain_info.get('registrant_country'),
			'phone': domain_info.get('registrant_phone'),
			'fax': domain_info.get('registrant_fax'),
			'email': domain_info.get('registrant_email'),
		},
		'admin': {
			'name': domain_info.get('admin_name'),
			'id': domain_info.get('admin_id'),
			'organization': domain_info.get('admin_organization'),
			'address':domain_info.get('admin_address'),
			'city': domain_info.get('admin_city'),
			'state': domain_info.get('admin_state'),
			'zipcode': domain_info.get('admin_zip_code'),
			'country': domain_info.get('admin_country'),
			'phone': domain_info.get('admin_phone'),
			'fax': domain_info.get('admin_fax'),
			'email': domain_info.get('admin_email'),
		},
		'technical_contact': {
			'name': domain_info.get('tech_name'),
			'id': domain_info.get('tech_id'),
			'organization': domain_info.get('tech_organization'),
			'address': domain_info.get('tech_address'),
			'city': domain_info.get('tech_city'),
			'state': domain_info.get('tech_state'),
			'zipcode': domain_info.get('tech_zip_code'),
			'country': domain_info.get('tech_country'),
			'phone': domain_info.get('tech_phone'),
			'fax': domain_info.get('tech_fax'),
			'email': domain_info.get('tech_email'),
		},
		'nameservers': domain_info.get('ns_records'),
		# 'similar_domains': domain_info.get('similar_domains'),
		'related_domains': domain_info.get('related_domains'),
		'related_tlds': domain_info.get('related_tlds'),
		'historical_ips': domain_info.get('historical_ips'),
	}


@app.task
def remove_duplicate_endpoints(
		scan_history_id,
		domain_id,
		subdomain_id=None,
		filter_ids=[],
		filter_status=[200, 301, 404],
		duplicate_removal_fields=ENDPOINT_SCAN_DEFAULT_DUPLICATE_FIELDS
	):
	"""Remove duplicate endpoints.

	Check for implicit redirections by comparing endpoints:
	- [x] `content_length` similarities indicating redirections
	- [x] `page_title` (check for same page title)
	- [ ] Sign-in / login page (check for endpoints with the same words)

	Args:
		scan_history_id: ScanHistory id.
		domain_id (int): Domain id.
		subdomain_id (int, optional): Subdomain id.
		filter_ids (list): List of endpoint ids to filter on.
		filter_status (list): List of HTTP status codes to filter on.
		duplicate_removal_fields (list): List of Endpoint model fields to check for duplicates
	"""
	logger.info(f'Removing duplicate endpoints based on {duplicate_removal_fields}')
	endpoints = (
		EndPoint.objects
		.filter(scan_history__id=scan_history_id)
		.filter(target_domain__id=domain_id)
	)
	if filter_status:
		endpoints = endpoints.filter(http_status__in=filter_status)

	if subdomain_id:
		endpoints = endpoints.filter(subdomain__id=subdomain_id)

	if filter_ids:
		endpoints = endpoints.filter(id__in=filter_ids)

	for field_name in duplicate_removal_fields:
		cl_query = (
			endpoints
			.values_list(field_name)
			.annotate(mc=Count(field_name))
			.order_by('-mc')
		)
		for (field_value, count) in cl_query:
			if count > DELETE_DUPLICATES_THRESHOLD:
				eps_to_delete = (
					endpoints
					.filter(**{field_name: field_value})
					.order_by('discovered_date')
					.all()[1:]
				)
				msg = f'Deleting {len(eps_to_delete)} endpoints [reason: same {field_name} {field_value}]'
				for ep in eps_to_delete:
					url = urlparse(ep.http_url)
					if url.path in ['', '/', '/login']: # try do not delete the original page that other pages redirect to
						continue
					msg += f'\n\t {ep.http_url} [{ep.http_status}] [{field_name}={field_value}]'
					ep.delete()
				logger.warning(msg)


@app.task
def run_command(cmd, cwd=None, shell=False, history_file=None, scan_id=None, activity_id=None):
	"""Run a given command using subprocess module.

	Args:
		cmd (str): Command to run.
		cwd (str): Current working directory.
		echo (bool): Log command.
		shell (bool): Run within separate shell if True.
		history_file (str): Write command + output to history file.

	Returns:
		tuple: Tuple with return_code, output.
	"""
	logger.info(cmd)
	logger.warning(activity_id)

	# Create a command record in the database
	command_obj = Command.objects.create(
		command=cmd,
		time=timezone.now(),
		scan_history_id=scan_id,
		activity_id=activity_id)

	# Run the command using subprocess
	popen = subprocess.Popen(
		cmd if shell else cmd.split(),
		shell=shell,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		cwd=cwd,
		universal_newlines=True)
	output = ''
	for stdout_line in iter(popen.stdout.readline, ""):
		item = stdout_line.strip()
		output += '\n' + item
		logger.debug(item)
	popen.stdout.close()
	popen.wait()
	return_code = popen.returncode
	command_obj.output = output
	command_obj.return_code = return_code
	command_obj.save()
	if history_file:
		mode = 'a'
		if not os.path.exists(history_file):
			mode = 'w'
		with open(history_file, mode) as f:
			f.write(f'\n{cmd}\n{return_code}\n{output}\n------------------\n')
	return return_code, output


#-------------#
# Other utils #
#-------------#

def stream_command(cmd, cwd=None, shell=False, history_file=None, encoding='utf-8', scan_id=None, activity_id=None, trunc_char=None):
	# Log cmd
	logger.info(cmd)
	# logger.warning(activity_id)

	# Create a command record in the database
	command_obj = Command.objects.create(
		command=cmd,
		time=timezone.now(),
		scan_history_id=scan_id,
		activity_id=activity_id)

	# Sanitize the cmd
	command = cmd if shell else cmd.split()

	# Run the command using subprocess
	process = subprocess.Popen(
		command,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		shell=shell)

	# Log the output in real-time to the database
	output = ""

	# Process the output
	for line in iter(lambda: process.stdout.readline() or process.stderr.readline(), b''):
		line = re.sub(r'\x1b[^m]*m', '', line.decode('utf-8').strip())
		if trunc_char and line.endswith(trunc_char):
			line = line[:-1]
		item = line

		# Try to parse the line as JSON
		try:
			item = json.loads(line)
		except json.JSONDecodeError:
			pass

		# Yield the line
		#logger.debug(item)
		yield item

		# Add the log line to the output
		output += line + "\n"

		# Update the command record in the database
		command_obj.output = output
		command_obj.save()

	# Retrieve the return code and output
	process.wait()
	return_code = process.returncode

	# Update the return code and final output in the database
	command_obj.return_code = return_code
	command_obj.save()

	# Append the command, return code and output to the history file
	if history_file is not None:
		with open(history_file, "a") as f:
			f.write(f"{cmd}\n{return_code}\n{output}\n")


def process_httpx_response(line):
	"""TODO: implement this"""


def extract_httpx_url(line):
	"""Extract final URL from httpx results. Always follow redirects to find
	the last URL.

	Args:
		line (dict): URL data output by httpx.

	Returns:
		tuple: (final_url, redirect_bool) tuple.
	"""
	status_code = line.get('status_code', 0)
	final_url = line.get('final_url')
	location = line.get('location')
	chain_status_codes = line.get('chain_status_codes', [])

	# Final URL is already looking nice, if it exists return it
	if final_url:
		return final_url, False
	http_url = line['url'] # fallback to url field

	# Handle redirects manually
	REDIRECT_STATUS_CODES = [301, 302]
	is_redirect = (
		status_code in REDIRECT_STATUS_CODES
		or
		any(x in REDIRECT_STATUS_CODES for x in chain_status_codes)
	)
	if is_redirect and location:
		if location.startswith(('http', 'https')):
			http_url = location
		else:
			http_url = f'{http_url}/{location.lstrip("/")}'

	# Sanitize URL
	http_url = sanitize_url(http_url)

	return http_url, is_redirect


#-------------#
# OSInt utils #
#-------------#

def get_and_save_dork_results(dork, type, host=None, scan_history=None, in_target=False):
	degoogle_obj = degoogle.dg()
	get_random_proxy()
	host = scan_history.domain.name if scan_history else host
	if in_target:
		query = f'{dork} site:{host}'
	else:
		query = f'{dork} \"{host}\"'
	degoogle_obj.query = query
	logger.info(f'Running degoogle with query "{query}" ...')
	results = degoogle_obj.run()
	dorks = []
	if not results:
		logger.warning('No data recovered from degoogle.')
		return []
	for result in results:
		dork, created = Dork.objects.get_or_create(
			type=type,
			description=result['desc'],
			url=result['url']
		)
		if created:
			logger.warning(f'Found dork {dork}')
		if scan_history:
			scan_history.dorks.add(dork)
		dorks.append(dork)
	return results


def get_and_save_emails(scan_history, results_dir):
	"""Get and save emails from Google, Bing and Baidu.

	Args:
		scan_history (startScan.ScanHistory): Scan history object.
		results_dir (str): Results directory.

	Returns:
		list: List of emails found.
	"""
	emails = []

	# Proxy settings
	get_random_proxy()

	# Gather emails from Google, Bing and Baidu
	try:
		logger.info('Getting emails from Google ...')
		email_from_google = get_emails_from_google(scan_history.domain.name)
		logger.info('Getting emails from Bing ...')
		email_from_bing = get_emails_from_bing(scan_history.domain.name)
		logger.info('Getting emails from Baidu ...')
		email_from_baidu = get_emails_from_baidu(scan_history.domain.name)
		emails = list(set(email_from_google + email_from_bing + email_from_baidu))
		logger.info(emails)
	except Exception as e:
		logger.exception(e)

	# Write to file
	output_path = f'{results_dir}/emails.txt'
	with open(output_path, 'w') as output_file:
		for email_address in emails:
			save_email(email_address, scan_history)
			output_file.write(f'{email_address}\n')

		# Fill output_file with possible email address
		output_file.write(f'%@{scan_history.domain.name}\n')
		output_file.write(f'%@%.{scan_history.domain.name}\n')
		output_file.write(f'%.%@{scan_history.domain.name}\n')
		output_file.write(f'%.%@%.{scan_history.domain.name}\n')
		output_file.write(f'%_%@{scan_history.domain.name}\n')
		output_file.write(f'%_%@%.{scan_history.domain.name}\n')

	return emails


def save_metadata_info(meta_dict):
	"""Extract metadata from Google Search.

	Args:
		meta_dict (dict): Info dict.

	Returns:
		list: List of startScan.MetaFinderDocument objects.
	"""
	logger.warning(f'Getting metadata for {meta_dict.osint_target}')

	# Proxy settings
	get_random_proxy()

	# Get metadata
	result = extract_metadata_from_google_search(meta_dict.osint_target, meta_dict.documents_limit)
	if not result:
		logger.error(f'No metadata result from Google Search for {meta_dict.osint_target}.')
		return []

	# Add metadata info to DB
	results = []
	for metadata_name, data in result.get_metadata().items():
		subdomain = Subdomain.objects.get(
			scan_history=meta_dict.scan_id,
			name=meta_dict.osint_target)
		metadata = DottedDict({k: v for k, v in data.items()})
		meta_finder_document = MetaFinderDocument(
			subdomain=subdomain,
			target_domain=meta_dict.domain,
			scan_history=meta_dict.scan_id,
			url=metadata.url,
			doc_name=metadata_name,
			http_status=metadata.status_code,
			producer=metadata.get('Producer'),
			creator=metadata.get('Creator'),
			creation_date=metadata.get('CreationDate'),
			modified_date=metadata.get('ModDate'),
			author=metadata.get('Author'),
			title=metadata.get('Title'),
			os=metadata.get('OSInfo'))
		meta_finder_document.save()
		results.append(data)
	return results


#-----------------#
# Utils functions #
#-----------------#

def create_scan_activity(scan_history_id, message, status):
	scan_activity = ScanActivity()
	scan_activity.scan_of = ScanHistory.objects.get(pk=scan_history_id)
	scan_activity.title = message
	scan_activity.time = timezone.now()
	scan_activity.status = status
	scan_activity.save()
	return scan_activity.id


#--------------------#
# Database functions #
#--------------------#


def save_vulnerability(**vuln_data):
	references = vuln_data.pop('references', [])
	cve_ids = vuln_data.pop('cve_ids', [])
	cwe_ids = vuln_data.pop('cwe_ids', [])
	tags = vuln_data.pop('tags', [])
	subscan = vuln_data.pop('subscan', None)

	# Create vulnerability
	vuln, created = Vulnerability.objects.get_or_create(**vuln_data)
	if created:
		vuln.discovered_date = timezone.now()
		vuln.open_status = True
		vuln.save()

	# Save vuln tags
	for tag_name in tags or []:
		tag, created = VulnerabilityTags.objects.get_or_create(name=tag_name)
		if tag:
			vuln.tags.add(tag)
			vuln.save()

	# Save CVEs
	for cve_id in cve_ids or []:
		cve, created = CveId.objects.get_or_create(name=cve_id)
		if cve:
			vuln.cve_ids.add(cve)
			vuln.save()

	# Save CWEs
	for cve_id in cwe_ids or []:
		cwe, created = CweId.objects.get_or_create(name=cve_id)
		if cwe:
			vuln.cwe_ids.add(cwe)
			vuln.save()

	# Save vuln reference
	for url in references or []:
		ref, created = VulnerabilityReference.objects.get_or_create(url=url)
		if created:
			vuln.references.add(ref)
			vuln.save()

	# Save subscan id in vuln object
	if subscan:
		vuln.vuln_subscan_ids.add(subscan)
		vuln.save()

	return vuln, created


def save_endpoint(
		http_url,
		ctx={},
		crawl=False,
		is_default=False,
		**endpoint_data):
	"""Get or create EndPoint object. If crawl is True, also crawl the endpoint
	HTTP URL with httpx.

	Args:
		http_url (str): Input HTTP URL.
		is_default (bool): If the url is a default url for SubDomains.
		scan_history (startScan.models.ScanHistory): ScanHistory object.
		domain (startScan.models.Domain): Domain object.
		subdomain (starScan.models.Subdomain): Subdomain object.
		results_dir (str, optional): Results directory.
		crawl (bool, optional): Run httpx on endpoint if True. Default: False.
		force (bool, optional): Force crawl even if ENABLE_HTTP_CRAWL mode is on.
		subscan (startScan.models.SubScan, optional): SubScan object.

	Returns:
		tuple: (startScan.models.EndPoint, created) where `created` is a boolean
			indicating if the object is new or already existed.
	"""
	scheme = urlparse(http_url).scheme
	endpoint = None
	created = False
	if crawl:
		ctx['track'] = False
		results = http_crawl(
			urls=[http_url],
			method='HEAD',
			ctx=ctx)
		if results:
			endpoint_data = results[0]
			endpoint_id = endpoint_data['endpoint_id']
			created = endpoint_data['endpoint_created']
			endpoint = EndPoint.objects.get(pk=endpoint_id)
	elif not scheme:
		return None, False
	else: # add dumb endpoint without probing it
		scan = ScanHistory.objects.filter(pk=ctx.get('scan_history_id')).first()
		domain = Domain.objects.filter(pk=ctx.get('domain_id')).first()
		if not validators.url(http_url):
			return None, False
		http_url = sanitize_url(http_url)
		endpoint, created = EndPoint.objects.get_or_create(
			scan_history=scan,
			target_domain=domain,
			http_url=http_url,
			**endpoint_data)

	if created:
		endpoint.is_default = is_default
		endpoint.discovered_date = timezone.now()
		endpoint.save()
		subscan_id = ctx.get('subscan_id')
		if subscan_id:
			endpoint.endpoint_subscan_ids.add(subscan_id)
			endpoint.save()

	return endpoint, created


def save_subdomain(subdomain_name, ctx={}):
	"""Get or create Subdomain object.

	Args:
		subdomain_name (str): Subdomain name.
		scan_history (startScan.models.ScanHistory): ScanHistory object.

	Returns:
		tuple: (startScan.models.Subdomain, created) where `created` is a
			boolean indicating if the object has been created in DB.
	"""
	scan_id = ctx.get('scan_history_id')
	subscan_id = ctx.get('subscan_id')
	out_of_scope_subdomains = ctx.get('out_of_scope_subdomains', [])
	valid_domain = (
		validators.domain(subdomain_name) or
		validators.ipv4(subdomain_name) or
		validators.ipv6(subdomain_name)
	)
	if not valid_domain:
		logger.error(f'{subdomain_name} is not an invalid domain. Skipping.')
		return None, False

	if subdomain_name in out_of_scope_subdomains:
		logger.error(f'{subdomain_name} is out-of-scope. Skipping.')
		return None, False

	scan = ScanHistory.objects.filter(pk=scan_id).first()
	domain = scan.domain if scan else None
	subdomain, created = Subdomain.objects.get_or_create(
		scan_history=scan,
		target_domain=domain,
		name=subdomain_name)
	if created:
		logger.warning(f'Found new subdomain {subdomain_name}')
		subdomain.discovered_date = timezone.now()
		if subscan_id:
			subdomain.subdomain_subscan_ids.add(subscan_id)
		subdomain.save()
	return subdomain, created


def save_email(email_address, scan_history=None):
	if not validators.email(email_address):
		logger.info(f'Email {email_address} is invalid. Skipping.')
		return None, False
	email, created = Email.objects.get_or_create(address=email_address)
	if created:
		logger.warning(f'Found new email address {email_address}')

	# Add email to ScanHistory
	if scan_history:
		scan_history.emails.add(email)
		scan_history.save()

	return email, created


def save_employee(name, designation, scan_history=None):
	employee, created = Employee.objects.get_or_create(
		name=name,
		designation=designation)
	if created:
		logger.warning(f'Found new employee {name}')

	# Add employee to ScanHistory
	if scan_history:
		scan_history.employees.add(employee)
		scan_history.save()

	return employee, created


def save_ip_address(ip_address, subdomain=None, subscan=None, **kwargs):
	if not (validators.ipv4(ip_address) or validators.ipv6(ip_address)):
		logger.info(f'IP {ip_address} is not a valid IP. Skipping.')
		return None, False
	ip, created = IpAddress.objects.get_or_create(address=ip_address)
	if created:
		logger.warning(f'Found new IP {ip_address}')

	# Set extra attributes
	for key, value in kwargs.items():
		setattr(ip, key, value)
	ip.save()

	# Add IP to subdomain
	if subdomain:
		subdomain.ip_addresses.add(ip)
		subdomain.save()

	# Add subscan to IP
	if subscan:
		ip.ip_subscan_ids.add(subscan)

	# Geo-localize IP asynchronously
	if created:
		geo_localize.delay(ip_address, ip.id)

	return ip, created


def save_imported_subdomains(subdomains, ctx={}):
	"""Take a list of subdomains imported and write them to from_imported.txt.

	Args:
		subdomains (list): List of subdomain names.
		scan_history (startScan.models.ScanHistory): ScanHistory instance.
		domain (startScan.models.Domain): Domain instance.
		results_dir (str): Results directory.
	"""
	domain_id = ctx['domain_id']
	domain = Domain.objects.get(pk=domain_id)
	results_dir = ctx.get('results_dir', RENGINE_RESULTS)

	# Validate each subdomain and de-duplicate entries
	subdomains = list(set([
		subdomain for subdomain in subdomains
		if validators.domain(subdomain) and domain.name == get_domain_from_subdomain(subdomain)
	]))
	if not subdomains:
		return

	logger.warning(f'Found {len(subdomains)} imported subdomains.')
	with open(f'{results_dir}/from_imported.txt', 'w+') as output_file:
		for name in subdomains:
			subdomain_name = name.strip()
			subdomain, _ = save_subdomain(subdomain_name, ctx=ctx)
			subdomain.is_imported_subdomain = True
			subdomain.save()
			output_file.write(f'{subdomain}\n')


@app.task
def query_reverse_whois(lookup_keyword):
	"""Queries Reverse WHOIS information for an organization or email address.

	Args:
		lookup_keyword (str): Registrar Name or email
	Returns:
		dict: Reverse WHOIS information.
	"""

	return get_associated_domains(lookup_keyword)


@app.task
def query_ip_history(domain):
	"""Queries the IP history for a domain

	Args:
		domain (str): domain_name
	Returns:
		list: list of historical ip addresses
	"""

	return get_domain_historical_ip_address(domain)


@app.task
def gpt_vulnerability_description(vulnerability_id):
	"""Generate and store Vulnerability Description using GPT.

	Args:
		vulnerability_id (Vulnerability Model ID): Vulnerability ID to fetch Description.
	"""
	logger.info('Getting GPT Vulnerability Description')
	try:
		lookup_vulnerability = Vulnerability.objects.get(id=vulnerability_id)
		lookup_url = urlparse(lookup_vulnerability.http_url)
		path = lookup_url.path
	except Exception as e:
		return {
			'status': False,
			'error': str(e)
		}

	# check in db GPTVulnerabilityReport model if vulnerability description and path matches
	stored = GPTVulnerabilityReport.objects.filter(url_path=path).filter(title=lookup_vulnerability.name).first()
	print(stored)
	if stored:
		response = {
			'status': True,
			'description': stored.description,
			'impact': stored.impact,
			'remediation': stored.remediation,
			'references': [url.url for url in stored.references.all()]
		}
	else:
		vulnerability_description = ''
		vulnerability_description += f'Vulnerability Title: {lookup_vulnerability.name}'
		# gpt gives concise vulnerability description when a vulnerable URL is provided
		vulnerability_description += f'\nVulnerable URL: {path}'
		# one can add more description here later

		gpt_generator = GPTVulnerabilityReportGenerator()
		response = gpt_generator.get_vulnerability_description(vulnerability_description)
		gpt_report = GPTVulnerabilityReport()
		gpt_report.url_path = path
		gpt_report.title = lookup_vulnerability.name
		gpt_report.description = response.get('description')
		gpt_report.impact = response.get('impact')
		gpt_report.remediation = response.get('remediation')
		gpt_report.save()

		for url in response.get('references', []):
			ref, created = VulnerabilityReference.objects.get_or_create(url=url)
			gpt_report.references.add(ref)
			gpt_report.save()


	# for all vulnerabilities with the same vulnerability name this description has to be stored.
	# also the consition is that the url must contain a part of this.

	for vuln in Vulnerability.objects.filter(name=lookup_vulnerability.name, http_url__icontains=path):
		vuln.description = response.get('description', vuln.description)
		vuln.impact = response.get('impact')
		vuln.remediation = response.get('remediation')
		vuln.is_gpt_used = True
		vuln.save()

		for url in response.get('references', []):
			ref, created = VulnerabilityReference.objects.get_or_create(url=url)
			vuln.references.add(ref)
			vuln.save()

	return response
