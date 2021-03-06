import logging
from flexget.plugin import register_plugin
from flexget.utils.template import RenderError

log = logging.getLogger('notifymyandroid')

__version__ = 0.1
headers = {'User-Agent': "FlexGet NMA plugin/%s" % str(__version__)}
url = 'https://nma.usk.bz/publicapi/notify'


class OutputNotifyMyAndroid(object):
    """
    Example::

      notifymyandroid:
        apikey: xxxxxxx
        [application: application name, default FlexGet]
        [event: event title, default New Release]
        [priority: -2 - 2 (2 = highest), default 0]

    Configuration parameters are also supported from entries (eg. through set).
    """

    def validator(self):
        from flexget import validator
        config = validator.factory('dict')
        config.accept('text', key='apikey', required=True)
        config.accept('text', key='application')
        config.accept('text', key='event')
        config.accept('integer', key='priority')
        config.accept('text', key='description')
        return config

    def prepare_config(self, config):
        if isinstance(config, bool):
            config = {'enabled': config}
        config.setdefault('application', 'FlexGet')
        config.setdefault('event', 'New release')
        config.setdefault('priority', 0)
        config.setdefault('description', '{{title}}')
        return config

    def on_task_output(self, task, config):
        # get the parameters
        config = self.prepare_config(config)
        for entry in task.accepted:

            if task.manager.options.test:
                log.info("Would send notifymyandroid message about: %s", entry['title'])
                continue

            apikey = entry.get('apikey', config['apikey'])
            priority = entry.get('priority', config['priority'])
            application = entry.get('application', config['application'])
            try:
                application = entry.render(application)
            except RenderError, e:
                log.error('Error setting nma application: %s' % e)
            event = entry.get('event', config['event'])
            try:
                event = entry.render(event)
            except RenderError, e:
                log.error('Error setting nma event: %s' % e)
            description = config['description']
            try:
                description = entry.render(description)
            except RenderError, e:
                log.error('Error setting nma description: %s' % e)

            # Send the request
            data = {'priority': priority, 'application': application, 'apikey': apikey,
                    'event': event, 'description': description}
            response = task.requests.post(url, headers=headers, data=data, raise_status=False)

            # Check if it succeeded
            request_status = response.status_code

            # error codes and messages from http://nma.usk.bz/api.php
            if request_status == 200:
                log.debug("NotifyMyAndroid message sent")
            elif request_status == 400:
                log.error("Bad request, the parameters you provided did not validate")
            elif request_status == 401:
                log.error("Not authorized, the API key given is not valid, and does not correspond to a user.")
            elif request_status == 402:
                log.error("Not acceptable, your IP address has exceeded the API limit.")
            elif request_status == 500:
                log.error("Internal server error, something failed to execute properly on the NotifyMyAndroid side.")
            else:
                log.error("Unknown error when sending NotifyMyAndroid message")

register_plugin(OutputNotifyMyAndroid, 'notifymyandroid', api_ver=2)
