# coding=utf-8

"""
Collects data from RabbitMQ through the admin interface

#### Notes
  ** With added support for breaking down queue metrics by vhost, we have
     attempted to keep results generated by existing configurations from
     changing. This means that the old behaviour of clobbering queue metrics
     when a single queue name exists in multiple vhosts still exists if the
     configuration is not updated. If no vhosts block is defined it will also
     keep the metric path as it was historically with no vhost name in it.

        old path => systems.myServer.rabbitmq.queues.myQueue.*
        new path => systems.myServer.rabbitmq.myVhost.queues.myQueue.*

  ** If a [vhosts] section exists but is empty, then no queues will be polled.
  ** To poll all vhosts and all queues, add the following.
  **   [vhosts]
  **   * = *
  **

#### Dependencies

 * pyrabbit

"""

import diamond.collector
import re
try:
    from numbers import Number
    import pyrabbit.api
    import pyrabbit.http
except ImportError:
    Number = None


class RabbitMQCollector(diamond.collector.Collector):

    def get_default_config_help(self):
        config_help = super(RabbitMQCollector, self).get_default_config_help()
        config_help.update({
            'host': 'Hostname and port to collect from',
            'user': 'Username',
            'password': 'Password',
            'queues': 'Queues to publish. Leave empty to publish all.',
            'vhosts':
            'A list of vhosts and queues for which we want to collect',
            'queues_ignored':
            'A list of queues or regexes for queue names not to report on.',
            'cluster':
            'If this node is part of a cluster, will collect metrics on the'
            ' cluster health'
        })
        return config_help

    def get_default_config(self):
        """
        Returns the default collector settings
        """
        config = super(RabbitMQCollector, self).get_default_config()
        config.update({
            'path':     'rabbitmq',
            'host':     'localhost:55672',
            'user':     'guest',
            'password': 'guest',
            'queues_ignored':   [],
            'cluster':  False,
        })
        return config

    def collect_health(self):
        health_metrics = [
            'fd_used',
            'fd_total',
            'mem_used',
            'mem_limit',
            'sockets_used',
            'sockets_total',
            'disk_free_limit',
            'disk_free',
            'proc_used',
            'proc_total',
            ]
        try:
            httpclient = pyrabbit.http.HTTPClient(self.config['host'],
                                                  self.config['user'],
                                                  self.config['password'])
            overview = httpclient.do_call('overview', 'GET')
            for metric in ['messages', 'messages_ready', 'messages_unacknowledged']:
                self.publish('health.{0}'.format(metric), overview['queue_totals'][metric])
            node_data = httpclient.do_call('nodes/{0}'.format(overview['node']), 'GET')
            for metric in health_metrics:
                self.publish('health.{0}'.format(metric), node_data[metric])
            if self.config['cluster']:
                self.publish('cluster.partitions', len(node_data['partitions']))
                content = httpclient.do_call('nodes', 'GET')
                self.publish('cluster.nodes', len(content))
        except Exception, e:
            self.log.error('Couldnt connect to rabbitmq %s', e)
            return {}

    def collect(self):
        if Number is None:
            self.log.error('Unable to import either Number or pyrabbit.api')
            return {}
        self.collect_health()
        matchers = []
        if self.config['queues_ignored']:
                for reg in self.config['queues_ignored']:
                    matchers.append(re.compile(reg))
        try:
            client = pyrabbit.api.Client(self.config['host'],
                                         self.config['user'],
                                         self.config['password'])

            legacy = False

            if 'vhosts' not in self.config:
                legacy = True

                if 'queues' in self.config:
                    self.config['vhosts'] = {"*": self.config['queues']}
                else:
                    self.config['vhosts'] = {"*": ""}

            # Legacy configurations, those that don't include the [vhosts]
            # section require special care so that we do not break metric
            # gathering for people that were using this collector before the
            # update to support vhosts.

            if not legacy:
                vhost_names = client.get_vhost_names()
                if "*" in self.config['vhosts']:
                    for vhost in vhost_names:
                        # Copy the glob queue list to each vhost not
                        # specifically defined in the configuration.
                        if vhost not in self.config['vhosts']:
                            self.config['vhosts'][vhost] = self.config[
                                'vhosts']['*']

                    del self.config['vhosts']["*"]

            # Iterate all vhosts in our vhosts configuration.  For legacy this
            # is "*" to force a single run.
            for vhost in self.config['vhosts']:
                queues = self.config['vhosts'][vhost]

                # Allow the use of a asterix to glob the queues, but replace
                # with a empty string to match how legacy config was.
                if queues == "*":
                    queues = ""
                allowed_queues = queues.split()

                # When we fetch queues, we do not want to define a vhost if
                # legacy.
                if legacy:
                    vhost = None

                for queue in client.get_queues(vhost):
                    # If queues are defined and it doesn't match, then skip.
                    if (queue['name'] not in allowed_queues
                            and len(allowed_queues) > 0):
                        continue
                    if matchers and any(
                            [m.match(queue['name']) for m in matchers]):
                        continue
                    for key in queue:
                        prefix = "queues"
                        if not legacy:
                            prefix = "vhosts.%s.%s" % (vhost, "queues")

                        name = '{0}.{1}'.format(prefix, queue['name'])
                        self._publish_metrics(name, [], key, queue)

            overview = client.get_overview()
            for key in overview:
                self._publish_metrics('', [], key, overview)
        except Exception, e:
            self.log.error('An error occurred collecting from RabbitMQ, %s', e)
            return {}

    def _publish_metrics(self, name, prev_keys, key, data):
        """Recursively publish keys"""
        value = data[key]
        keys = prev_keys + [key]
        if isinstance(value, dict):
            for new_key in value:
                self._publish_metrics(name, keys, new_key, value)
        elif isinstance(value, Number):
            joined_keys = '.'.join(keys)
            if name:
                publish_key = '{0}.{1}'.format(name, joined_keys)
            else:
                publish_key = joined_keys
            if isinstance(value, bool):
                value = int(value)

            self.publish(publish_key, value)
