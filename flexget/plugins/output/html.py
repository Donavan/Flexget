import os
import logging
from flexget.plugin import register_plugin
from flexget.utils.template import render_from_task, get_template

PLUGIN_NAME = 'make_html'

log = logging.getLogger(PLUGIN_NAME)


class OutputHtml:

    def validator(self):
        from flexget import validator
        root = validator.factory('dict')
        root.accept('text', key='template')
        root.accept('text', key='file', required=True)
        return root

    def on_task_output(self, task, config):
        # Use the default template if none is specified
        if not config.get('template'):
            config['template'] = 'default.template'

        filename = os.path.expanduser(config['template'])
        output = os.path.expanduser(config['file'])
        # Output to config directory if absolute path has not been specified
        if not os.path.isabs(output):
            output = os.path.join(task.manager.config_base, output)

        # create the template
        template = render_from_task(get_template(filename, PLUGIN_NAME), task)

        log.verbose('Writing output html to %s' % output)
        f = open(output, 'w')
        f.write(template.encode('utf-8'))
        f.close()

register_plugin(OutputHtml, PLUGIN_NAME, api_ver=2)
