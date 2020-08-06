import os
import sh
import sys
import argparse
import subprocess
from enum import Enum
from tempfile import NamedTemporaryFile
from nginx.config.api import Config, Section, Location


BASE_PORT = '8000'
ANCHOR_START = """### START AUTO MANAGE ###"""
ANCHOR_STOP = """### STOP AUTO MANAGE ###"""

IPS = ['127.0.0.{}'.format(i) for i in range(1, 250)]

parser = argparse.ArgumentParser()
parser.add_argument('--no-open', action='store_true')
parser.add_argument('--name')

args = parser.parse_args()


class Mode(Enum):
    NONE = ''
    NORMAL = '\033[0m'
    OPERATION = '\033[94m'
    OK = '\033[92m'
    FAIL = '\033[91m'
    WARNING = '\033[93m'
    INFO = '\033[93m'
    BOLD = '\033[1m'


def pprint(text, mode=Mode.OK, end='\n', continuous=False):

    marker = '{} OK '.format(Mode.BOLD.value)

    if mode == Mode.FAIL:
        marker = '{}FAIL'.format(Mode.BOLD.value)

    elif mode == Mode.INFO:
        marker = ' ~  '

    elif mode == Mode.OPERATION:
        marker = ' >> '

    to_write = '{}{} {}{}'.format(mode.value, marker, text, Mode.NORMAL.value)

    if continuous:
        print('{}{}{}'.format(mode.value, text, Mode.NORMAL.value), end=end)

    else:
        print(to_write, end=end)


def get_active_djangos():
    try:
        active_djangos = sh.grep.bake('-Po')(sh.ps('-aux', _piped=True), '(?<=/bin/python manage.py runserver )[0-9.:]+')
        return list(filter(lambda x: x, active_djangos))
    except Exception:
        return []


def get_managed_host():

    def all_etc_host():
        with open('/etc/hosts', 'r') as etc:
            return etc.read()

    managed_host = {}
    managed = False

    for line in all_etc_host().split('\n'):

        if managed and line != ANCHOR_STOP:
            ip, name = line.split('\t')
            managed_host[name] = ip

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

    sh.sudo('service', 'nginx', 'reload')


def get_tmux_windows_name():
    tmux_name = sh.tmux.bake('display-message', '-p')('"#W"').split('\n', 1)[0].replace('"', '')
    return tmux_name


def get_managepy_file():
    location = None
    for filename in os.listdir(os.getcwd()):
        if filename == 'manage.py':
            location = os.path.join(os.getcwd(), filename)

    return location


def is_django_active(ip):
    return ip in get_active_djangos()


def django_server_activate(endpoint):
    sh.python('manage.py', 'runserver', '{}:{}'.format(endpoint, BASE_PORT))


def main():

    skip_creation = False

    active_hosts = get_managed_host()
    active_tmux_windows = get_tmux_windows_name()

    if not args.name:
        server_endpoint = 'local.{}'.format(active_tmux_windows)

    else:
        server_endpoint = args.name

    location_managepy = get_managepy_file()

    if not location_managepy:
        pprint('No manage.py file found !', Mode.FAIL)
        skip_creation = True

    else:
        pprint('Manage.py file found: {}'.format(location_managepy))

    if server_endpoint in active_hosts:
        pprint('Host already in config for this name!', Mode.FAIL)
        pprint('Ip for {} is {}'.format(server_endpoint, active_hosts[server_endpoint]), Mode.INFO)

        if not is_django_active(active_hosts[server_endpoint]) and location_managepy:
            django_server_activate(active_hosts[server_endpoint])

        if not args.no_open:
            pprint('Opening {}'.format(active_hosts[server_endpoint]), Mode.OPERATION)
            sh.firefox('http://{}'.format(server_endpoint))
        sys.exit(1)

    if not skip_creation:
        pprint('Set name for server as "{}"'.format(server_endpoint))

        choosen_ip = search_free_dev_ip()

        pprint('Creating /etc/hosts config for {} @ {}'.format(server_endpoint, choosen_ip))

        active_hosts[server_endpoint] = choosen_ip
        update_managed_host(active_hosts)

        pprint('Creating Nginx config for {} @ {}'.format(server_endpoint, choosen_ip))

        nginx_config = create_nginx_config(active_hosts)
        update_nginx_config(nginx_config)

        if not args.no_open:
            pprint('Opening the endpoint in firefox, since \'--open\' is passed', Mode.OPERATION)
            sh.firefox('http://{}'.format(server_endpoint))

        pprint('Running django server {} @ {}'.format(server_endpoint, choosen_ip))

        django_server_activate(active_hosts[server_endpoint])


main()
