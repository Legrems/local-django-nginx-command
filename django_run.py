from GenericClass.Task import CommandTask


import re
import os
import sh
import io
import sys
import time
import copy
import string
import psutil
import argparse
import subprocess
from enum import Enum
from tempfile import NamedTemporaryFile
from nginx.config.api.options import KeyValuesMultiLines
from nginx.config.api import Config, Section, Location, KeyValueOption


BASE_PORT = '8000'
ANCHOR_START = """### START AUTO MANAGE ###"""
ANCHOR_STOP = """### STOP AUTO MANAGE ###"""

IPS = ['127.0.0.{}'.format(i) for i in range(2, 250)]

SERVER_NAME_FORMAT = "{}.local"

parser = argparse.ArgumentParser()
parser.add_argument('--no-open', action="store_true")
parser.add_argument('--name', help=("A name for the local host: XXX will be available on XXX.local. If there is no name parameters, the window's name on the tmux session is taken by default"))
parser.add_argument('--raw-name', help=("A raw name for the local host: XXX will be available on XXX."))
parser.add_argument('--suffix-name', help=("Suffix name to the env found: {env}.{suffix}.local"))
parser.add_argument('--prefix-name', help=("Prefix name to the env found: {prefix}.{env}.local"))
parser.add_argument('--see-tmux-name', action="store_true", help=("See your tmux window's name, see --name for why."))
parser.add_argument('--clear', action="store_true", help=("Clear managed host and managed nginx config"))
parser.add_argument('--confirm-clear', action="store_true", help=("Needed to confirm clear, see --clear"))
parser.add_argument('--erase', type=str, nargs="+", help=("Erase an entry in etc/hosts as well as in nginx"))
parser.add_argument('--managed', action="store_true", help=("Display managed django server"))
parser.add_argument('--config', action="store_true", help=("Display nginx managed config"))
parser.add_argument('--open-all', action="store_true", help=("Open all active django server"))
parser.add_argument('--reload-config', action="store_true", help=("Rewrite managed host and nginx config"))
parser.add_argument('--remove', nargs="+", type=str, help=("Remove a entry in the host list, case insensitive"))
parser.add_argument('--celery', action="store_true", help=("Run celery subprocess"))
parser.add_argument('--sls', action="store_true", help=("Generate SLS command for gestion/gestion-extra"))
parser.add_argument('--use-tmux-window-name', action="store_true", help=("Use the tmux window name for the name, instead of the env/project name"))

args = parser.parse_args()


class FirefoxAsyncLaunch(CommandTask):

    def function(self, endpoint, time=2, *args, **kwargs):
        time.sleep(time)
        sh.firefox(endpoint)


class CeleryWorker(CommandTask):

    def function(self, endpoint, *args, **kwargs):
        command_string = "celery -A app worker -l info -B -E -n {}"
        worker_name = "worker-{}".format(endpoint.replace(".local", ""))
        pprint("Launching celery with worker name {}".format(worker_name))
        subprocess.Popen(command_string.format(worker_name).split(" "), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Mode(Enum):
    NONE = ''
    NORMAL = '\033[0m'
    OPERATION = '\033[94m'
    OK = '\033[92m'
    FAIL = '\033[91m'
    WARNING = '\033[93m'
    INFO = '\033[93m'
    BOLD = '\033[1m'
    INTEROGATION = '\033[36m'


def pprint(text, mode=Mode.OK, end='\n', continuous=False):

    marker = '{} OK '.format(Mode.BOLD.value)

    if mode == Mode.FAIL:
        marker = '{}FAIL'.format(Mode.BOLD.value)

    elif mode == Mode.INFO:
        marker = ' ~  '

    elif mode == Mode.OPERATION:
        marker = ' >> '

    elif mode == Mode.WARNING:
        marker = ' !! '

    elif mode == Mode.INTEROGATION:
        marker = ' ?? '

    to_write = '{}{} {}{}'.format(mode.value, marker, text, Mode.NORMAL.value)

    if continuous:
        print('{}{}{}'.format(mode.value, text, Mode.NORMAL.value), end=end)

    else:
        print(to_write, end=end)


def get_active_djangos(get_name=False):

    active_djangos = []
    for proc in psutil.process_iter(['cmdline']):

        cmdline = ' '.join(proc.info['cmdline'])
        if 'manage.py runserver' in cmdline and "bin/python" in cmdline:
            django_ip = proc.info['cmdline'][-1]

            if get_name:
                short_location = proc.info['cmdline'][0].replace(os.path.join(os.getenv("HOME"), 'miniconda3/envs/'), '').replace('/bin/python', '')
                active_djangos.append({'ip': django_ip, 'location': short_location})

            else:
                active_djangos.append(django_ip)

    return active_djangos


def get_managed_host():

    def all_etc_host():
        with open('/etc/hosts', 'r') as etc:
            return etc.read()

    managed_host = {}
    managed = False

    for line in all_etc_host().split('\n'):

        if managed and line != ANCHOR_STOP:
            try:
                ip, name = line.split('\t')
                managed_host[name] = ip
            except Exception:
                pass

        if line == ANCHOR_START:
            managed = True

        if line == ANCHOR_STOP:
            managed = False

    return managed_host


def update_managed_host(hosts):
    middle = '\\n'.join(['{}\t{}'.format(v, k) for k, v in hosts.items()])
    replaced = '{}\\n{}\\n{}'.format(ANCHOR_START, middle, ANCHOR_STOP)
    sed_command = ["sudo", "sed", "-i", "-n", "/{}/{{:a;N;/{}/!ba;N;s/.*\\n/{}\\n/}};p".format(ANCHOR_START, ANCHOR_STOP, replaced), "/etc/hosts"]

    process = subprocess.Popen(sed_command, stdout=subprocess.PIPE)


def search_free_dev_ip():
    all_hosts = get_managed_host()

    choosen_ip = ''
    pprint('Searching ip ...')

    for ip in IPS:

        if ip not in {v: k for k, v in all_hosts.items()}:
            pprint('    {} <=='.format(ip), continuous=True)
            choosen_ip = ip
            break

        pprint('    {} X'.format(ip), Mode.FAIL, continuous=True)

    return choosen_ip


def create_nginx_config(hosts):
    sections = [
        Section(
            'server',
            Location(
                '/',
                include='proxy_params',
                proxy_pass='http://{}:{}'.format(value, BASE_PORT)
            ),
            listen='80',
            server_name=key
        ) for key, value in hosts.items()
    ]

    if False:  # Old and false
        sections.append(
            Section(
                'server',
                *(Location(
                    '/{}/'.format(key.replace(SERVER_NAME_FORMAT.format(''), '')),
                    KeyValueOption('include', 'proxy_params'),
                    KeyValueOption('rewrite', '^/{}(/.*)$ http://{}$1 last'.format(key.replace(SERVER_NAME_FORMAT.format(''), ''), key)),
                    KeyValueOption('proxy_pass', 'http://{}:{}'.format(value, BASE_PORT)),
                    KeyValueOption('proxy_redirect', 'http://{}:{}/ $scheme://$host:$server_port/'.format(value, BASE_PORT)),
                    KeyValuesMultiLines('proxy_set_header', [
                        ['X-Real-IP', '$remote_addr'],
                        ['X-Forwarded-For', '$proxy_add_x_forwarded_for'],
                        # ['Host', 'django.local'],
                        ['X-Forwarded-Proto', '$scheme'],
                    ]),
                ) for key, value in hosts.items()),
                listen='80',
                server_name=SERVER_NAME_FORMAT.format('django'),
            )
        )

    sections.append(
        Section(
            "server",
            *[
                Location(
                    "/",
                    # KeyValueOption("rewrite", "^/{}(/.*)$ $1 break".format(key.rstrip(SERVER_NAME_FORMAT.format("")))),
                    # KeyValueOption("proxy_pass", "http://{}:{}/".format(value, BASE_PORT)),
                    # KeyValueOption("proxy_redirect", "http://{}:{}/ /".format(value, BASE_PORT)),
                    # KeyValueOption("proxy_redirect", "$scheme://$host:$server_port/ /test/"),
                    # KeyValueOption("proxy_set_header", "X-Real-IP $remote_addr"),
                    # KeyValueOption("proxy_set_header", "X-Forwarded-For $proxy_add_x_forwarded_for"),
                    # KeyValueOption("proxy_set_header", "Host $host"),
                    # KeyValueOption("proxy_set_header", "X-Forwarded-Proto $scheme"),
                    # KeyValueOption("proxy_buffering", "off"),
                    # KeyValueOption("proxy_http_version", "1.1"),
                    # KeyValueOption("proxy_request_buffering", "off"),
                    KeyValueOption("include", "proxy_params"),
                    KeyValueOption("proxy_pass", "http://127.0.0.2:8000/"),
                ) for key, value in hosts.items()
            ],
            listen="80",
            server_name=SERVER_NAME_FORMAT.format("django"),
            client_max_body_size=0,
        )
    )

    return '\n'.join(map(str, sections))


def update_nginx_config(config):
    nginx_file = '/etc/nginx/sites-available/managed'

    temp_name = ''

    pprint('Write the nginx config in a temporary file')
    with NamedTemporaryFile(delete=False) as temp:
        temp.write(config.encode('utf-8'))

        temp_name = temp.name

    pprint('Removing the actual nginx config file "{}"'.format(nginx_file))

    sh.sudo('rm', nginx_file)

    pprint('Copying the temporary file {} into the nginx conf'.format(temp_name))

    sh.sudo('cp', temp_name, nginx_file)

    pprint('Reloading nginx service')

    # sh.sudo('service', 'nginx', 'reload')
    sh.sudo('systemctl', 'restart', 'nginx')


def get_tmux_windows_name():
    tmux_name = sh.tmux.bake('display-message', '-p')('"#W"').split('\n', 1)[0].replace('"', '')
    return tmux_name


def get_project_name():
    def normalize(txt):
        return txt

    folder = os.getcwd().split("/")
    name = "Documents"
    if name in folder:
        return folder[folder.index(name) + 2]

    # If we cannot find a project name, try with the venv name
    sp = sys.path[1].split('/')
    if "env" in sp:
        return normalize(sp[sp.index("env") + 1])

    return "default"


def get_managepy_file():
    def search(folder):
        location = None
        for filename in os.listdir(folder):
            if filename.endswith("manage.py"):
                location = os.path.join(folder, filename)

        return location

    folder = os.getcwd()
    location = search(folder)

    if not location:
        for file in os.listdir(folder):
            filename = os.path.join(folder, file)
            if os.path.isdir(filename):
                loc = search(filename)
                if loc:
                    location = loc

    return location


def is_django_active(endpoint):
    active_djangos = get_active_djangos()

    if endpoint not in active_djangos:
        return False

    return '{}:{}'.format(ip, BASE_PORT) in active_djangos()


def django_server_activate(managepy_location, endpoint, commands=[]):
    for c in commands:
        pprint("Running command: {}".format(c))
        subprocess.call(c.split(" "))

    subprocess.call(['python', managepy_location, 'runserver_plus', '{}:{}'.format(endpoint, BASE_PORT)])


def clear_all():
    pprint('Removing /etc/hosts managed configs')
    update_managed_host({})
    pprint('Removing nginx managed config')
    update_nginx_config('')


def main():

    skip_creation = False

    choosen_ip = ''

    active_hosts = get_managed_host()

    print(get_active_djangos())

    if args.use_tmux_window_name:
        active_tmux_windows = get_tmux_windows_name()

    else:
        active_tmux_windows = get_project_name()

    if args.clear:
        if not args.confirm_clear:
            pprint("It's gonna erase ALL managed host, and ALL managed nginx config, use --confirm-clear to to so", Mode.WARNING)
            sys.exit(0)

        else:
            clear_all()
            sys.exit(0)

    if args.managed:
        pprint('Managed hosts: ', Mode.OPERATION)

        for host, ip in active_hosts.items():

            text = '{} @ {}'.format(host, ip)
            pprint(text)

        pprint('Found running django servers: ', Mode.OPERATION)
        for running_django in get_active_djangos(get_name=True):

            pprint(running_django['location'], continuous=True, end='')
            pprint(" : with IP: http://", Mode.OPERATION, continuous=True, end='')
            pprint(running_django['ip'], continuous=True)

        pprint('Normaly, those are correct and running:', Mode.OPERATION)

        for host, ip in active_hosts.items():
            for running_django in get_active_djangos(get_name=True):
                if '{}:{}'.format(ip, BASE_PORT) == running_django['ip']:
                    pprint('{}{}{} - {}{: <30}{} => {}(env){} {}{}{}'.format(
                        Mode.BOLD.value,
                        ip,
                        Mode.NORMAL.value,
                        Mode.INFO.value,
                        host,
                        Mode.NORMAL.value,
                        Mode.BOLD.value,
                        Mode.NORMAL.value,
                        Mode.OPERATION.value,
                        running_django['location'],
                        Mode.NORMAL.value
                    ))

        sys.exit(0)

    if args.config:
        pprint('Normal nginx config file:', Mode.OPERATION)
        normal_nginx_config = create_nginx_config(active_hosts)

        pprint(normal_nginx_config, Mode.NORMAL, continuous=True)
        sys.exit(0)

    if args.open_all:
        for host in get_active_djangos(get_name=True):
            pprint('Opening [{}] endpoint in firefox [IP={}]'.format(host["location"], host["ip"]))

            FirefoxAsyncLaunch('http://{}'.format(SERVER_NAME_FORMAT.format(host["location"]))).start()

        sys.exit(0)

    if args.remove:
        pprint("Following host will be erased:", Mode.WARNING)
        old_active_hosts = copy.deepcopy(active_hosts)

        for to_remove in args.remove:
            for host, ip in old_active_hosts.items():
                # Remove if name match
                if to_remove.lower() in host:
                    pprint("Removing {} with ip {}".format(host, ip))

                    active_hosts.pop(host, None)

                # Or ip
                if to_remove.lower() in ip:
                    pprint("Removing {} with ip {}".format(host, ip))

                    active_hosts.pop(host, None)

        if not args.reload_config:
            pprint("No host realy removed, to apply change, use --reload-config to write down the config")

    if args.erase:

        for to_be_erased in args.erase:
            if active_hosts.get(to_be_erased):
                active_hosts.pop(to_be_erased)
                pprint(f"Removing {to_be_erased} from /etc/hosts and nginx config")

        update_managed_host(active_hosts)
        update_nginx_config(create_nginx_config(active_hosts))
        sys.exit(0)

    if args.reload_config:

        update_managed_host(active_hosts)
        update_nginx_config(create_nginx_config(active_hosts))
        sys.exit(0)

    if args.see_tmux_name:
        pprint("Tmux window's name: {}, server host: {}".format(active_tmux_windows, SERVER_NAME_FORMAT.format(active_tmux_windows)), Mode.INFO)

    which_python = sh.which("python")
    rmatch = re.match(".*\/miniconda3\/envs\/(?P<env>.*)\/bin\/python", which_python)

    if rmatch:
        env = rmatch.groupdict().get("env")

        if args.prefix_name:
            env = f"{args.prefix_name}.{env}"

        if args.suffix_name:
            env = f"{env}.{args.suffix_name}"

        server_endpoint = SERVER_NAME_FORMAT.format(env)

    else:
        server_endpoint = SERVER_NAME_FORMAT.format(active_tmux_windows)

    if args.name:
        server_endpoint = SERVER_NAME_FORMAT.format(args.name)

    if args.raw_name:
        server_endpoint = args.raw_name

    location_managepy = get_managepy_file()

    if not location_managepy:
        pprint('No manage.py file found !', Mode.FAIL)
        skip_creation = True

    else:
        pprint('Manage.py file found: {}'.format(location_managepy))

    if server_endpoint in active_hosts:
        skip_creation = True

        pprint('Host already in config for this name!', Mode.FAIL)
        pprint('Ip for {} is {}'.format(server_endpoint, active_hosts[server_endpoint]), Mode.INFO)

        choosen_ip = active_hosts[server_endpoint]

    if not skip_creation:
        choosen_ip = search_free_dev_ip()

        pprint('Creating /etc/hosts config for {} @ {}'.format(server_endpoint, choosen_ip))

        active_hosts[server_endpoint] = choosen_ip
        update_managed_host(active_hosts)

        pprint('Creating Nginx config for {} @ {}'.format(server_endpoint, choosen_ip))

        nginx_config = create_nginx_config(active_hosts)
        update_nginx_config(nginx_config)

    is_active = is_django_active(server_endpoint)

    if not args.no_open and is_active:
        pprint('Opening the endpoint in firefox, since \'--no-open\' is not passed', Mode.OPERATION)

        FirefoxAsyncLaunch('http://{}'.format(server_endpoint)).start()

    if not is_active and location_managepy:
        pprint('Opening the endpoint in firefox, since \'--no-open\' is not passed', Mode.OPERATION)

        FirefoxAsyncLaunch('http://{}'.format(server_endpoint)).start()

        pprint('Running django server {} @ {}'.format(server_endpoint, choosen_ip))

        if args.celery:
            CeleryWorker(server_endpoint).start()

        pre_command = []
        commands = []
        if args.sls:
            commands.append(f"python {location_managepy} generate_sls tmp_states -yy")
            commands.append(f"python {location_managepy} generate_extra_sls tmp_states_extra -yy")

        django_server_activate(location_managepy, active_hosts[server_endpoint], commands)

    elif not location_managepy:
        pprint('Exiting ...', Mode.FAIL)

    else:
        pprint('Django server already running!')


main()
