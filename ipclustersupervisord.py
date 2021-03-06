# Adopted by Cory Dolphin from Justin Riley's work
# This file is part of the StarClusterReliableIPCluster plugin.
#
# The StarClusterReliableIPCluster is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# the StarClusterReliableIPCluster is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with the StarClusterReliableIPCluster. If not, see <http://www.gnu.org/licenses/>.

"""
A starcluster plugin for running an IPython cluster
using supervisord to watch processes and restart as necessary.
(requires IPython 0.13+)
"""
import json
import os
import time
import posixpath

from starcluster import utils
from starcluster import static
from starcluster import spinner
from starcluster import exception
from starcluster.utils import print_timing
from starcluster.clustersetup import DefaultClusterSetup
from starcluster.plugins.ipcluster import IPCluster
from starcluster.logger import log

IPCLUSTER_CACHE = os.path.join(static.STARCLUSTER_CFG_DIR, 'ipcluster')
CHANNEL_NAMES = (
    "control",
    "task",
    "notification",
    "mux",
    "iopub",
    "registration",
)

STARTED_MSG = """\
IPClusterSupervisord has been started on %(cluster)s for user '%(user)s'
with %(n_engines)d engines on %(n_nodes)d nodes.

To connect to cluster from your local machine use:

from IPython.parallel import Client
client = Client('%(connector_file)s', sshkey='%(key_location)s')

See the IPCluster plugin doc for usage details:
http://star.mit.edu/cluster/docs/latest/plugins/ipython.html
"""


def _start_engines(node, user, n_engines=None, kill_existing=True):
    """Launch IPython engines on the given node

    Start one engine per CPU except on master where 1 CPU is reserved for house
    keeping tasks when possible.

    If kill_existing is True, any running of IPython engines on the same node
    are killed first.

    """
    node.ssh.switch_user(user)
    if kill_existing:
        node.ssh.execute("pkill -f supervisord", ignore_exit_status=True)
        node.ssh.execute("pkill -f IPython.parallel.engine", ignore_exit_status=True)
    node.ssh.execute("supervisord -c /home/%s/.engine.conf" % user)
    node.ssh.switch_user('root')


class ReliableIPCluster(IPCluster):
    """Start an IPython (>= 0.13) cluster reliably! 
    Example config:

    [plugin ipcluster]
    setup_class = starcluster.plugins.ipcluster.IPCluster
    enable_notebook = True
    notebook_passwd = secret
    notebook_directory = /home/user/notebooks
    packer = pickle
    log_level = info
    n_engines_per_node = 1 (optional, defauls to num_processors)
    n_engines_master = 1 (optional, defauls to num_processors - 1)
    """
    def __init__(self, enable_notebook=False, notebook_passwd=None,
                 notebook_directory=None, packer=None, n_engines_per_node=None,
                 n_engines_master=None,
                 hub_db_class='IPython.parallel.controller.dictdb.NoDB', log_level='INFO'):
        super(ReliableIPCluster, self).__init__()
        if isinstance(enable_notebook, basestring):
            self.enable_notebook = enable_notebook.lower().strip() == 'true'
        else:
            self.enable_notebook = enable_notebook
        self.notebook_passwd = notebook_passwd or utils.generate_passwd(16)
        self.notebook_directory = notebook_directory

        self.hub_db_class = hub_db_class
        if n_engines_per_node is None:
            self.n_engines_per_node = None
        else:
            self.n_engines_per_node = int(n_engines_per_node)

        if n_engines_master is None:
            self.n_engines_master = None
        else:
            self.n_engines_master = int(n_engines_master)

        self.log_level = log_level
        if packer not in (None, 'json', 'pickle', 'msgpack'):
            log.error("Unsupported packer: %s", packer)
            self.packer = None
        else:
            self.packer = packer

    def _check_ipython_installed(self, node):
        has_ipy = node.ssh.has_required(['ipython', 'ipcluster','supervisord'])
        if not has_ipy:
            raise exception.PluginError("IPython is not installed!")
        return has_ipy

    def _write_config(self, master, user, profile_dir):
        """Create cluster configuration files."""
        log.info("Writing IPython cluster config files")
        master.ssh.execute("rm -rf '%s'" % profile_dir)
        master.ssh.execute("rm '.ipengine.conf'", ignore_exit_status=True)
        master.ssh.execute("rm '.ipcontroller.conf'", ignore_exit_status=True)
        master.ssh.execute('ipython profile create')
        f = master.ssh.remote_file('%s/ipcontroller_config.py' % profile_dir)
        ssh_server = "@".join([user, master.public_dns_name])
        f.write('\n'.join([
            "c = get_config()",
            "c.HubFactory.ip='%s'" % master.private_ip_address,
            "c.IPControllerApp.ssh_server='%s'" % ssh_server,
            "c.Application.log_level = '%s'" % self.log_level,
            "c.HubFactory.db_class = '%s'" % self.hub_db_class,
            "",
        ]))
        f.close()
        f = master.ssh.remote_file('%s/ipengine_config.py' % profile_dir)
        f.write('\n'.join([
            "c = get_config()",
            "c.EngineFactory.timeout = 10",
            # Engines should wait a while for url files to arrive,
            # in case Controller takes a bit to start:
            "c.IPEngineApp.wait_for_url_file = 30",
            "c.Application.log_level = '%s'" % self.log_level,
            "",
        ]))
        f.close()
        f = master.ssh.remote_file('%s/ipython_config.py' % profile_dir)
        f.write('\n'.join([
            "c = get_config()",
            "c.EngineFactory.timeout = 10",
            # Engines should wait a while for url files to arrive,
            # in case Controller takes a bit to start
            "c.IPEngineApp.wait_for_url_file = 30",
            "c.Application.log_level = '%s'" % self.log_level,
            "",
        ]))
        if self.packer == 'msgpack':
            f.write('\n'.join([
                "c.Session.packer='msgpack.packb'",
                "c.Session.unpacker='msgpack.unpackb'",
                "",
            ]))
        elif self.packer == 'pickle':
            f.write('\n'.join([
                "c.Session.packer='pickle'",
                "",
            ]))
        # else: use the slow default JSON packer
        f.close()
        log.info("Writing supervisord config files")
        f = master.ssh.remote_file('/home/%s/.engine.conf' % user)
        f.write('\n'.join([
            "[supervisord]",
            "logfile = /tmp/engine.log",
            "loglevel = info",
            "pidfile = /tmp/engine.pid",
            "",
            "[program:ipengine]",
            "command=ipcluster engines --n=%i" % self.n_engines_per_node,
            "directory=/home/%s" % user,
            "user=%s" % user,
            "autorestart=true",
            "stdout_logfile=/home/sgeadmin/.ipython/profile_default/stdout_engine_%(host_node_name)s_%(process_num)s.log",
            "stderr_logfile=/home/sgeadmin/.ipython/profile_default/stderr_engine_%(host_node_name)s_%(process_num)s.log",
            "",
        ]))
        f.close()
        f = master.ssh.remote_file('/home/%s/.controller.conf' % user)
        f.write('\n'.join([
            "[supervisord]",
            "logfile = /tmp/ipcontroller.log",
            "loglevel = info",
            "pidfile = /tmp/ipcontroller.pid",
            "",
            "[program:ipcontroller]",
            "command=ipcluster start --n=0 --delay=5",
            "directory=/home/%s" % user,
            "user=%s" % user,
            "autorestart=true",
            "stdout_logfile=/home/sgeadmin/.ipython/profile_default/stdout_controller_%(host_node_name)s_%(process_num)s.log",
            "stderr_logfile=/home/sgeadmin/.ipython/profile_default/stderr_controller_%(host_node_name)s_%(process_num)s.log",
            "",
        ]))
        f.close()

    def _start_cluster(self, master,user, profile_dir):
        if self.n_engines_master is None:
            self.n_engines_master = max(1, master.num_processors - 1)

        log.info("Starting supervisord for ipcontroller on master")


        master.ssh.execute("pkill -f ipcontrollerapp",
                           ignore_exit_status=True)
        # cleanup existing connection files, to prevent their use
        master.ssh.execute("rm -f %s/security/*.json" % profile_dir)
        master.ssh.execute("supervisord -c /home/%s/.controller.conf" % user)
        # wait for JSON file to exist
        json_filename = '%s/security/ipcontroller-client.json' % profile_dir
        log.info("Waiting for JSON connector file...",
                 extra=dict(__nonewline__=True))
        s = spinner.Spinner()
        s.start()
        try:
            found_file = False
            for i in range(30):
                if master.ssh.isfile(json_filename):
                    found_file = True
                    break
                time.sleep(1)
            if not found_file:
                raise ValueError(
                    "Timeout while waiting for the cluser json file: "
                    + json_filename)
        finally:
            s.stop()
        # Retrieve JSON connection info to make it possible to connect a local
        # client to the cluster controller
        if not os.path.isdir(IPCLUSTER_CACHE):
            log.info("Creating IPCluster cache directory: %s" %
                     IPCLUSTER_CACHE)
            os.makedirs(IPCLUSTER_CACHE)
        local_json = os.path.join(IPCLUSTER_CACHE,
                                  '%s-%s.json' % (master.parent_cluster,
                                                  master.region.name))
        master.ssh.get(json_filename, local_json)
        # Configure security group for remote access
        connection_params = json.load(open(local_json, 'rb'))
        # For IPython version 0.14+ the list of channel ports is explicitly
        # provided in the connector file
        channel_authorized = False
        for channel in CHANNEL_NAMES:
            port = connection_params.get(channel)
            if port is not None:
                self._authorize_port(master, port, channel)
                channel_authorized = True
        # For versions prior to 0.14, the channel port numbers are not given in
        # the connector file: let's open everything in high port numbers
        if not channel_authorized:
            self._authorize_port(master, (1000, 65535), "IPython controller")
        return local_json, self.n_engines_master

    def _start_notebook(self, master, user, profile_dir):
        log.info("Setting up IPython web notebook for user: %s" % user)
        user_cert = posixpath.join(profile_dir, '%s.pem' % user)
        ssl_cert = posixpath.join(profile_dir, '%s.pem' % user)
        if not master.ssh.isfile(user_cert):
            log.info("Creating SSL certificate for user %s" % user)
            ssl_subj = "/C=US/ST=SC/L=STAR/O=Dis/CN=%s" % master.dns_name
            master.ssh.execute(
                "openssl req -new -newkey rsa:4096 -days 365 "
                '-nodes -x509 -subj %s -keyout %s -out %s' %
                (ssl_subj, ssl_cert, ssl_cert))
        else:
            log.info("Using existing SSL certificate...")
        f = master.ssh.remote_file('%s/ipython_notebook_config.py' %
                                   profile_dir)
        notebook_port = 8888
        sha1py = 'from IPython.lib import passwd; print passwd("%s")'
        sha1cmd = "python -c '%s'" % sha1py
        sha1pass = master.ssh.execute(sha1cmd % self.notebook_passwd)[0]
        f.write('\n'.join([
            "c = get_config()",
            "c.IPKernelApp.pylab = 'inline'",
            "c.NotebookApp.certfile = u'%s'" % ssl_cert,
            "c.NotebookApp.ip = '*'",
            "c.NotebookApp.open_browser = False",
            "c.NotebookApp.password = u'%s'" % sha1pass,
            "c.NotebookApp.port = %d" % notebook_port,
        ]))
        f.close()
        if self.notebook_directory is not None:
            if not master.ssh.path_exists(self.notebook_directory):
                master.ssh.makedirs(self.notebook_directory)
            master.ssh.execute_async(
                "ipython notebook --no-browser --notebook-dir='%s'"
                % self.notebook_directory)
        else:
            master.ssh.execute_async("ipython notebook --no-browser")
        self._authorize_port(master, notebook_port, 'notebook')
        log.info("IPython notebook URL: https://%s:%s" %
                 (master.dns_name, notebook_port))
        log.info("The notebook password is: %s" % self.notebook_passwd)
        log.warn("Please check your local firewall settings if you're having "
                 "issues connecting to the IPython notebook",
                 extra=dict(__textwrap__=True))

    def _authorize_port(self, node, port, service_name, protocol='tcp'):
        group = node.cluster_groups[0]
        world_cidr = '0.0.0.0/0'
        if isinstance(port, tuple):
            port_min, port_max = port
        else:
            port_min, port_max = port, port
        port_open = node.ec2.has_permission(group, protocol, port_min,
                                            port_max, world_cidr)
        if not port_open:
            log.info("Authorizing tcp ports [%s-%s] on %s for: %s" %
                     (port_min, port_max, world_cidr, service_name))
            node.ec2.conn.authorize_security_group(
                group_id=group.id, ip_protocol='tcp', from_port=port_min,
                to_port=port_max, cidr_ip=world_cidr)

    @print_timing("IPCluster")
    def run(self, nodes, master, user, user_shell, volumes):
        self._check_ipython_installed(master)
        user_home = master.getpwnam(user).pw_dir
        profile_dir = posixpath.join(user_home, '.ipython', 'profile_default')
        master.ssh.switch_user(user)
        self._write_config(master, user, profile_dir)
        # Start the cluster and some engines on the master (leave 1
        # processor free to handle cluster house keeping)
        cfile, n_engines_master = self._start_cluster(master, user, profile_dir)
        # Start engines on each of the non-master nodes
        non_master_nodes = [node for node in nodes if not node.is_master()]

        n_engines_non_master = 0
        for node in non_master_nodes:
            num_engines = self.n_engines_per_node or node.num_processors
            self.pool.simple_job(
                _start_engines, (node, user, num_engines),
                jobid=node.alias)
            n_engines_non_master += num_engines

        if len(non_master_nodes) > 0:
            log.info("Adding %d engines on %d nodes",
                     n_engines_non_master, len(non_master_nodes))
            self.pool.wait(len(non_master_nodes))
        if self.enable_notebook:
            self._start_notebook(master, user, profile_dir)
        n_engines_total = n_engines_master + n_engines_non_master
        log.info(STARTED_MSG % dict(cluster=master.parent_cluster,
                                    user=user, connector_file=cfile,
                                    key_location=master.key_location,
                                    n_engines=n_engines_total,
                                    n_nodes=len(nodes)))
        master.ssh.switch_user('root')

    def on_add_node(self, node, nodes, master, user, user_shell, volumes):
        log.info("Starting supervisord on new node %s" % (node.alias))
        _start_engines(node,user)


class ReliableIPClusterStop(DefaultClusterSetup):
    """Shutdown all the IPython processes of the cluster

    This plugin is meant to be run manually with:

      starcluster runplugin plugin_conf_name cluster_name

    """
    def run(self, nodes, master, user, user_shell, volumes):
        log.info("Shutting down IPython cluster")
        master.ssh.switch_user(user)
        log.info("Stopping IPython controller on %s", master.alias)
        master.ssh.execute("ipcluster stop", ignore_exit_status=True)
        log.info("Waiting 5 seconds for the process to restart itself")
        time.sleep(5)
        master.ssh.execute("pkill -f 'ipython notebook'",
                           ignore_exit_status=True)
        master.ssh.switch_user('root')
        log.info("Stopping IPython engines on %d nodes", len(nodes))
        for node in nodes:
            self.pool.simple_job(self._stop_engines, (node, user))
        self.pool.wait(len(nodes))

    def _stop_engines(self, node, user):
        node.ssh.switch_user(user)
        node.ssh.execute("pkill -f supervisord", ignore_exit_status=True)
        node.ssh.execute("pkill -f IPython.parallel.engine", ignore_exit_status=True)
        node.ssh.switch_user('root')

    def on_add_node(self, node, nodes, master, user, user_shell, volumes):
        raise NotImplementedError("on_add_node method not implemented")

    def on_remove_node(self, node, nodes, master, user, user_shell, volumes):
        raise NotImplementedError("on_remove_node method not implemented")


class ReliableIPClusterRestartEngines(DefaultClusterSetup):
    """Plugin to kill and restart all engines of an IPython cluster

    This plugin can be useful to hard-reset the all the engines, for instance
    to be sure to free all the used memory even when dealing with memory leaks
    in compiled extensions.

    This plugin is meant to be run manually with:

      starcluster runplugin plugin_conf_name cluster_name
    

    Example config:    
    [plugin restartipcluster]
    setup_class = starcluster.plugins.ipcluster.IPClusterRestartEngines
    n_engines_per_node = 1 (optional, defauls to num_processors)
    n_engines_master = 1 (optional, defauls to num_processors - 1)

    """
    def __init__(self, n_engines_per_node=None,
                 n_engines_master=None, log_level='INFO'):
        super(ReliableIPClusterRestartEngines, self).__init__()

        if n_engines_per_node is None:
            self.n_engines_per_node = None
        else:
            self.n_engines_per_node = int(n_engines_per_node)

        if n_engines_master is None:
            self.n_engines_master = None
        else:
            self.n_engines_master = int(n_engines_master)


    def run(self, nodes, master, user, user_shell, volumes):
        master.ssh.switch_user(user)

        log.info("Stopping IPython controller on %s", master.alias)
        master.ssh.execute("pkill -f ipcontroller", ignore_exit_status=True)
        time.sleep(2)
        master.ssh.execute("pkill -f 'ipython notebook'",
                           ignore_exit_status=True)

        n_total = 0
        for node in nodes:
            if node.is_master():
                continue # Skip master node, assume no engines on master.
            n_engines = self.n_engines_per_node or node.num_processors
            self.pool.simple_job(
                _start_engines, (node, user, n_engines, True),
                jobid=node.alias)
            n_total += n_engines
        log.info("Restarting engines on %d nodes", len(nodes))
        self.pool.wait(len(nodes))

    def on_add_node(self, node, nodes, master, user, user_shell, volumes):
        raise NotImplementedError("on_add_node method not implemented")

    def on_remove_node(self, node, nodes, master, user, user_shell, volumes):
        raise NotImplementedError("on_remove_node method not implemented")
