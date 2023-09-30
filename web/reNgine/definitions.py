#!/usr/bin/python
import logging
import re

###############################################################################
# TOOLS DEFINITIONS
###############################################################################
logger = logging.getLogger('django')

###############################################################################
# TOOLS DEFINITIONS
###############################################################################

EMAIL_REGEX = re.compile(r'[a-z0-9\.\-+_]+@[a-z0-9\.\-+_]+\.[a-z]+')

###############################################################################
# YAML CONFIG DEFINITIONS
###############################################################################

ALL = 'all'
AMASS_WORDLIST = 'amass_wordlist'
AUTO_CALIBRATION = 'auto_calibration'
CUSTOM_HEADER = 'custom_header'
DIR_FILE_FUZZ = 'dir_file_fuzz'
FOLLOW_REDIRECT = 'follow_redirect'
EXTENSIONS = 'extensions'
EXCLUDED_SUBDOMAINS = 'excluded_subdomains'
EXCLUDE_EXTENSIONS = 'exclude_extensions'
EXCLUDE_TEXT = 'exclude_text'
FETCH_URL = 'fetch_url'
GF_PATTERNS = 'gf_patterns'
HTTP_CRAWL = 'http_crawl'
IGNORE_FILE_EXTENSION = 'ignore_file_extensions'
INTENSITY = 'intensity'
MATCH_HTTP_STATUS = 'match_http_status'
MAX_TIME = 'max_time'
NAABU_EXCLUDE_PORTS = 'exclude_ports'
NAABU_EXCLUDE_SUBDOMAINS = 'exclude_subdomains'
ENABLE_NMAP = 'enable_nmap'
NMAP_COMMAND = 'nmap_cmd'
NMAP_SCRIPT = 'nmap_script'
NMAP_SCRIPT_ARGS = 'nmap_script_args'
NAABU_PASSIVE = 'passive'
NAABU_RATE = 'rate'
NUCLEI_CUSTOM_TEMPLATE = 'custom_templates'
NUCLEI_TAGS = 'tags'
NUCLEI_TEMPLATE = 'templates'
NUCLEI_SEVERITY = 'severity'
NUCLEI_CONCURRENCY = 'concurrency'
OSINT = 'osint'
OSINT_DOCUMENTS_LIMIT = 'documents_limit'
OSINT_DISCOVER = 'discover'
OSINT_DORK = 'dork'
PORT = 'port'
PORTS = 'ports'
RECURSIVE = 'recursive'
RECURSIVE_LEVEL = 'recursive_level'
PORT_SCAN = 'port_scan'
RATE_LIMIT = 'rate_limit'
RETRIES = 'retries'
SCREENSHOT = 'screenshot'
SUBDOMAIN_DISCOVERY = 'subdomain_discovery'
STOP_ON_ERROR = 'stop_on_error'
ENABLE_HTTP_CRAWL = 'enable_http_crawl'
THREADS = 'threads'
TIMEOUT = 'timeout'
USE_AMASS_CONFIG = 'use_amass_config'
USE_NAABU_CONFIG = 'use_naabu_config'
USE_NUCLEI_CONFIG = 'use_nuclei_config'
USE_SUBFINDER_CONFIG = 'use_subfinder_config'
USES_TOOLS = 'uses_tools'
VULNERABILITY_SCAN = 'vulnerability_scan'
WAF_DETECTION = 'waf_detection'
WORDLIST = 'wordlist'
REMOVE_DUPLICATE_ENDPOINTS = 'remove_duplicate_endpoints'
DUPLICATE_REMOVAL_FIELDS = 'duplicate_fields'
DALFOX = 'dalfox'
WAF_EVASION = 'waf_evasion'
BLIND_XSS_SERVER = 'blind_xss_server'
USER_AGENT = 'blind_xss_server'
DELAY = 'delay'

###############################################################################
# Scan DEFAULTS
###############################################################################

LIVE_SCAN = 1
SCHEDULED_SCAN = 0

DEFAULT_SCAN_INTENSITY = 'normal'

###############################################################################
# Tools DEFAULTS
###############################################################################

# amass
AMASS_DEFAULT_WORDLIST_PATH = (
    'wordlist/default_wordlist/deepmagic.com-prefixes-top50000.txt'
)

# dorks
DORKS_DEFAULT_NAMES = [
    'stackoverflow',
    '3rdparty',
    'social_media',
    'project_management',
    'code_sharing',
    'config_files',
    'jenkins',
    'cloud_buckets',
    'php_error',
    'exposed_documents',
    'struts_rce',
    'db_files',
    'traefik',
    'git_exposed'
]

# ffuf
FFUF_DEFAULT_WORDLIST_PATH = '/usr/src/wordlist/dicc.txt'
FFUF_DEFAULT_MATCH_HTTP_STATUS = [200, 204]
FFUF_DEFAULT_RECURSIVE_LEVEL = 2
FFUF_DEFAULT_FOLLOW_REDIRECT = False

# naabu
NAABU_DEFAULT_PORTS = ['top-100']

# nuclei
NUCLEI_DEFAULT_TEMPLATES_PATH = '/root/nuclei-templates'
NUCLEI_SEVERITY_MAP = {
    'info': 0,
    'low': 1,
    'medium': 2,
    'high': 3,
    'critical': 4,
    'unknown': -1,
}
NUCLEI_REVERSE_SEVERITY_MAP = {v: k for k, v in NUCLEI_SEVERITY_MAP.items()}
NUCLEI_DEFAULT_SEVERITIES = list(NUCLEI_SEVERITY_MAP.keys())

# dalfox
DALFOX_SEVERITY_MAP = {
    'Low': 1,
    'Medium': 2,
    'High': 3,
    'unknown': -1,
}

# osint
OSINT_DEFAULT_LOOKUPS = ['emails', 'metainfo', 'employees']
OSINT_DEFAULT_DORKS = [
    'stackoverflow',
    '3rdparty',
    'social_media',
    'project_management',
    'code_sharing',
    'config_files',
    'jenkins',
    'wordpress_files',
    'cloud_buckets',
    'php_error',
    'exposed_documents',
    'struts_rce',
    'db_files',
    'traefik',
    'git_exposed',
]
OSINT_DEFAULT_CONFIG = {
    'discover': OSINT_DEFAULT_LOOKUPS,
    'dork': OSINT_DEFAULT_DORKS
}

# subdomain scan
SUBDOMAIN_SCAN_DEFAULT_TOOLS = ['subfinder', 'ctfr', 'sublist3r', 'tlsx']

# endpoints scan
ENDPOINT_SCAN_DEFAULT_TOOLS = ['gospider']
ENDPOINT_SCAN_DEFAULT_DUPLICATE_FIELDS = ['content_length', 'page_title']


###############################################################################
# Logger DEFINITIONS
###############################################################################

CONFIG_FILE_NOT_FOUND = 'Config file not found'

###############################################################################
# Preferences DEFINITIONS
###############################################################################

SMALL = '100px'
MEDIM = '200px'
LARGE = '400px'
XLARGE = '500px'

# Discord message colors
DISCORD_INFO_COLOR = '0xfbbc00' # yellow
DISCORD_WARNING_COLOR = '0xf75b00' # orange
DISCORD_ERROR_COLOR = '0xf70000'
DISCORD_SUCCESS_COLOR = '0x00ff78'
DISCORD_SEVERITY_COLORS = {
    'info': DISCORD_INFO_COLOR,
    'warning': DISCORD_WARNING_COLOR,
    'error': DISCORD_ERROR_COLOR,
    'aborted': DISCORD_ERROR_COLOR,
    'success': DISCORD_SUCCESS_COLOR
}

STATUS_TO_SEVERITIES = {
    'RUNNING': 'info',
    'SUCCESS': 'success',
    'FAILED': 'error',
    'ABORTED': 'error'
}

###############################################################################
# Interesting Subdomain DEFINITIONS
###############################################################################
MATCHED_SUBDOMAIN = 'Subdomain'
MATCHED_PAGE_TITLE = 'Page Title'

###############################################################################
# Celery Task Status CODES
###############################################################################
INITIATED_TASK = -1
FAILED_TASK = 0
RUNNING_TASK = 1
SUCCESS_TASK = 2
ABORTED_TASK = 3

CELERY_TASK_STATUS_MAP = {
    INITIATED_TASK: 'INITITATED',
    FAILED_TASK: 'FAILED',
    RUNNING_TASK: 'RUNNING',
    SUCCESS_TASK: 'SUCCESS',
    ABORTED_TASK: 'ABORTED'
}

CELERY_TASK_STATUSES = (
    (INITIATED_TASK, INITIATED_TASK),
    (FAILED_TASK, FAILED_TASK),
    (RUNNING_TASK, RUNNING_TASK),
    (SUCCESS_TASK, SUCCESS_TASK),
    (ABORTED_TASK, ABORTED_TASK)
)
DYNAMIC_ID = -1

###############################################################################
# Uncommon Ports
# Source: https://github.com/six2dez/reconftw/blob/main/reconftw.cfg
###############################################################################
UNCOMMON_WEB_PORTS = [
    81,
    300,
    591,
    593,
    832,
    981,
    1010,
    1311,
    1099,
    2082,
    2095,
    2096,
    2480,
    3000,
    3128,
    3333,
    4243,
    4567,
    4711,
    4712,
    4993,
    5000,
    5104,
    5108,
    5280,
    5281,
    5601,
    5800,
    6543,
    7000,
    7001,
    7396,
    7474,
    8000,
    8001,
    8008,
    8014,
    8042,
    8060,
    8069,
    8080,
    8081,
    8083,
    8088,
    8090,
    8091,
    8095,
    8118,
    8123,
    8172,
    8181,
    8222,
    8243,
    8280,
    8281,
    8333,
    8337,
    8443,
    8500,
    8834,
    8880,
    8888,
    8983,
    9000,
    9001,
    9043,
    9060,
    9080,
    9090,
    9091,
    9200,
    9443,
    9502,
    9800,
    9981,
    10000,
    10250,
    11371,
    12443,
    15672,
    16080,
    17778,
    18091,
    18092,
    20720,
    32000,
    55440,
    55672,
]

###############################################################################
# WHOIS DEFINITIONS
# IGNORE_WHOIS_RELATED_KEYWORD: To ignore and disable finding generic related domains
###############################################################################

IGNORE_WHOIS_RELATED_KEYWORD = [
    'Registration Private',
    'Domains By Proxy Llc',
    'Redacted For Privacy',
    'Digital Privacy Corporation',
    'Private Registrant',
    'Domain Administrator',
    'Administrator',
]


# Default FETCH URL params
DEFAULT_IGNORE_FILE_EXTENSIONS = [
    'png',
    'jpg',
    'jpeg',
    'gif',
    'mp4',
    'mpeg',
    'mp3',
]

DEFAULT_GF_PATTERNS = [
    'debug_logic',
    'idor',
    'interestingEXT',
    'interestingparams',
    'interestingsubs',
    'lfi',
    'rce',
    'redirect',
    'sqli',
    'ssrf',
    'ssti',
    'xss'
]


# Default Dir File Fuzz Params
DEFAULT_DIR_FILE_FUZZ_EXTENSIONS =  [
    'php',
    'git',
    'yaml',
    'conf',
    'cnf',
    'config',
    'gz',
    'env',
    'log',
    'db',
    'mysql',
    'bak',
    'asp',
    'aspx',
    'txt',
    'conf',
    'sql',
    'json',
    'yml',
    'pdf',
]

# Roles and Permissions
PERM_MODIFY_SYSTEM_CONFIGURATIONS = 'modify_system_configurations'
PERM_MODIFY_SCAN_CONFIGURATIONS = 'modify_scan_configurations'
PERM_MODIFY_TARGETS = 'modify_targets'
PERM_MODIFY_SCAN_RESULTS = 'modify_scan_results'
PERM_MODIFY_WORDLISTS = 'modify_wordlists'
PERM_MODIFY_INTERESTING_LOOKUP = 'modify_interesting_lookup'
PERM_MODIFY_SCAN_REPORT = 'modify_scan_report'
PERM_INITATE_SCANS_SUBSCANS = 'initiate_scans_subscans'

# 404 page url
FOUR_OH_FOUR_URL = '/404/'


# GPT Vulnerability Report Generator
VULNERABILITY_DESCRIPTION_SYSTEM_MESSAGE = """
    You are a penetration tester, You will be given with a Vulnerability title and some description.
    Your job is to write a detailed penetration testing report based on the Vulnerability details.

    The penetration testing report must contain

    - Vulnerability description
    - Impact
    - Remediation
    - References

    Do not write 'Penetration Testing Report:' on the title
"""
