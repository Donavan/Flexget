import os
import logging
import urlparse
import xml.sax
import posixpath
import httplib
from datetime import datetime
import feedparser
from requests import RequestException
from flexget.entry import Entry
from flexget.plugin import register_plugin, internet, PluginError
from flexget.utils.cached_input import cached
from flexget.utils.tools import decode_html

log = logging.getLogger('rss')


class InputRSS(object):
    """
    Parses RSS feed.

    Hazzlefree configuration for public rss feeds::

      rss: <url>

    Configuration with basic http authentication::

      rss:
        url: <url>
        username: <name>
        password: <password>

    Advanced usages:

    You may wish to clean up the entry by stripping out all non-ascii characters.
    This can be done by setting ascii value to yes.

    Example::

      rss:
        url: <url>
        ascii: yes

    In case RSS-feed uses some nonstandard field for urls and automatic detection fails
    you can configure plugin to use url from any feedparser entry attribute.

    Example::

      rss:
        url: <url>
        link: guid

    If you want to keep information in another rss field attached to the flexget entry, you can use the other_fields option.

    Example::

      rss:
        url: <url>
        other_fields: [date]

    You can disable few possibly annoying warnings by setting silent value to
    yes on feeds where there are frequently invalid items.

    Example::

      rss:
        url: <url>
        silent: yes

    You can group all the links of an item, to make the download plugin tolerant
    to broken urls: it will try to download each url until one works.
    Links are enclosures plus item fields given by the link value, in that order.
    The value to set is "group_links".

    Example::

      rss:
        url: <url>
        group_links: yes
    """

    def validator(self):
        from flexget import validator
        root = validator.factory()
        root.accept('url')
        root.accept('file')
        advanced = root.accept('dict')
        advanced.accept('url', key='url', required=True)
        advanced.accept('file', key='url')
        advanced.accept('text', key='username')
        advanced.accept('text', key='password')
        advanced.accept('text', key='title')
        advanced.accept('text', key='link')
        advanced.accept('list', key='link').accept('text')
        other_fields = advanced.accept('list', key='other_fields')
        other_fields.accept('text')
        other_fields.accept('dict').accept_any_key('text')
        advanced.accept('boolean', key='silent')
        advanced.accept('boolean', key='ascii')
        advanced.accept('boolean', key='filename')
        advanced.accept('boolean', key='group_links')
        advanced.accept('boolean', key='all_entries')
        return root

    def build_config(self, config):
        """Set default values to config"""
        if isinstance(config, basestring):
            config = {'url': config}
        # set the default link value to 'auto'
        config.setdefault('link', 'auto')
        # Replace : with _ and lower case other fields so they can be found in rss
        if config.get('other_fields'):
            other_fields = []
            for item in config['other_fields']:
                if isinstance(item, basestring):
                    key, val = item, item
                else:
                    key, val = item.items()[0]
                other_fields.append({key.replace(':', '_').lower(): val.lower()})
            config['other_fields'] = other_fields
        # set default value for group_links as deactivated
        config.setdefault('group_links', False)
        # set default for all_entries
        config.setdefault('all_entries', False)
        return config

    def process_invalid_content(self, task, data):
        """If feedparser reports error, save the received data and log error."""

        if data is None:
            log.critical('Received empty page - no content')
            return
        ext = 'xml'
        if '<html>' in data.lower():
            log.critical('Received content is HTML page, not an RSS feed')
            ext = 'html'
        if 'login' in data.lower() or 'username' in data.lower():
            log.critical('Received content looks a bit like login page')
        if 'error' in data.lower():
            log.critical('Received content looks a bit like error page')
        received = os.path.join(task.manager.config_base, 'received')
        if not os.path.isdir(received):
            os.mkdir(received)
        filename = os.path.join(received, '%s.%s' % (task.name, ext))
        f = open(filename, 'w')
        f.write(data)
        f.close()
        log.critical('I have saved the invalid content to %s for you to view' % filename)

    def add_enclosure_info(self, entry, enclosure, filename=True, multiple=False):
        """Stores information from an rss enclosure into an Entry."""
        entry['url'] = enclosure['href']
        # get optional meta-data
        if 'length' in enclosure:
            try:
                entry['size'] = int(enclosure['length'])
            except:
                entry['size'] = 0
        if 'type' in enclosure:
            entry['type'] = enclosure['type']
        # TODO: better and perhaps join/in download plugin?
        # Parse filename from enclosure url
        basename = posixpath.basename(urlparse.urlsplit(entry['url']).path)
        # If enclosure has size OR there are multiple enclosures use filename from url
        if (entry.get('size') or multiple and basename) and filename:
            entry['filename'] = basename
            log.trace('filename `%s` from enclosure' % entry['filename'])

    @cached('rss')
    @internet(log)
    def on_task_input(self, task, config):
        config = self.build_config(config)

        log.debug('Requesting task `%s` url `%s`' % (task.name, config['url']))

        # Used to identify which etag/modified to use
        url_hash = str(hash(config['url']))

        # set etag and last modified headers if config has not changed since
        # last run and if caching wasn't disabled with --no-cache argument.
        all_entries = config['all_entries'] or task.config_modified or task.manager.options.nocache
        headers = {}
        if not all_entries:
            etag = task.simple_persistence.get('%s_etag' % url_hash, None)
            if etag:
                log.debug('Sending etag %s for task %s' % (etag, task.name))
                headers['If-None-Match'] = etag
            modified = task.simple_persistence.get('%s_modified' % url_hash, None)
            if modified:
                if not isinstance(modified, basestring):
                    log.debug('Invalid date was stored for last modified time.')
                else:
                    headers['If-Modified-Since'] = modified
                    log.debug('Sending last-modified %s for task %s' % (headers['If-Modified-Since'], task.name))

        # Get the feed content
        if config['url'].startswith(('http', 'https', 'ftp', 'file')):
            # Get feed using requests library
            auth = None
            if 'username' in config and 'password' in config:
                auth = (config['username'], config['password'])
            try:
                # Use the raw response so feedparser can read the headers and status values
                response = task.requests.get(config['url'], timeout=60, headers=headers, raise_status=False, auth=auth)
                content = response.content
            except RequestException, e:
                raise PluginError('Unable to download the RSS for task %s (%s): %s' %
                                  (task.name, config['url'], e))

            # status checks
            status = response.status_code
            if status == 304:
                log.verbose('%s hasn\'t changed since last run. Not creating entries.' % config['url'])
                # Let details plugin know that it is ok if this feed doesn't produce any entries
                task.no_entries_ok = True
                return []
            elif status == 401:
                raise PluginError('Authentication needed for task %s (%s): %s' %\
                                  (task.name, config['url'], response.headers['www-authenticate']), log)
            elif status == 404:
                raise PluginError('RSS Feed %s (%s) not found' % (task.name, config['url']), log)
            elif status == 500:
                raise PluginError('Internal server exception on task %s (%s)' % (task.name, config['url']), log)
            elif status != 200:
                raise PluginError('HTTP error %s received from %s' % (status, config['url']), log)

            # update etag and last modified
            if not config['all_entries']:
                etag = response.headers.get('etag')
                if etag:
                    task.simple_persistence['%s_etag' % url_hash] = etag
                    log.debug('etag %s saved for task %s' % (etag, task.name))
                if  response.headers.get('last-modified'):
                    modified = response.headers['last-modified']
                    task.simple_persistence['%s_modified' % url_hash] = modified
                    log.debug('last modified %s saved for task %s' % (modified, task.name))
        else:
            # This is a file, open it
            content = open(config['url'], 'rb').read()

        if not content:
            log.error('No data recieved for rss feed.')
            return
        try:
            rss = feedparser.parse(content)
        except LookupError, e:
            raise PluginError('Unable to parse the RSS (from %s): %s' % (config['url'], e))

        # check for bozo
        ex = rss.get('bozo_exception', False)
        ignore = False
        if ex:
            if isinstance(ex, feedparser.NonXMLContentType):
                # see: http://www.feedparser.org/docs/character-encoding.html#advanced.encoding.nonxml
                log.debug('ignoring feedparser.NonXMLContentType')
                ignore = True
            elif isinstance(ex, feedparser.CharacterEncodingOverride):
                # see: ticket 88
                log.debug('ignoring feedparser.CharacterEncodingOverride')
                ignore = True
            elif isinstance(ex, UnicodeEncodeError):
                if rss.entries:
                    log.info('Feed has UnicodeEncodeError but seems to produce entries, ignoring the error ...')
                    ignore = True
            elif isinstance(ex, xml.sax._exceptions.SAXParseException):
                if not rss.entries:
                    # save invalid data for review, this is a bit ugly but users seem to really confused when
                    # html pages (login pages) are received
                    self.process_invalid_content(task, content)
                    if task.manager.options.debug:
                        log.exception(ex)
                    raise PluginError('Received invalid RSS content from task %s (%s)' % (task.name, config['url']))
                else:
                    msg = ('Invalid XML received (%s). However feedparser still produced entries.'
                           ' Ignoring the error...' % str(ex).replace('<unknown>:', 'line '))
                    if not config.get('silent', False):
                        log.info(msg)
                    else:
                        log.debug(msg)
                    ignore = True
            elif isinstance(ex, httplib.BadStatusLine) or isinstance(ex, IOError):
                raise ex # let the @internet decorator handle
            else:
                # all other bozo errors
                if not rss.entries:
                    self.process_invalid_content(task, content)
                    raise PluginError('Unhandled bozo_exception. Type: %s (task: %s)' %\
                                      (ex.__class__.__name__, task.name), log)
                else:
                    msg = 'Invalid RSS received. However feedparser still produced entries. Ignoring the error ...'
                    if not config.get('silent', False):
                        log.info(msg)
                    else:
                        log.debug(msg)

        if 'bozo' in rss:
            if rss.bozo and not ignore:
                log.error(rss)
                log.error('Bozo exception %s on task %s' % (type(ex), task.name))
                return
        else:
            log.warn('feedparser bozo bit missing, feedparser bug? (FlexGet ticket #721)')

        log.debug('encoding %s' % rss.encoding)

        last_entry_id = ''
        if not all_entries:
            # Test to make sure entries are in descending order
            if rss.entries and rss.entries[0].get('published_parsed'):
                if rss.entries[0]['published_parsed'] < rss.entries[-1]['published_parsed']:
                    # Sort them if they are not
                    rss.entries.sort(key=lambda x: x['published_parsed'], reverse=True)
            last_entry_id = task.simple_persistence.get('%s_last_entry' % url_hash)

        # new entries to be created
        entries = []

        # field name for url can be configured by setting link.
        # default value is auto but for example guid is used in some feeds
        ignored = 0
        for entry in rss.entries:

            # Check if title field is overridden in config
            title_field = config.get('title', 'title')
            # ignore entries without title
            if not entry.get(title_field):
                log.debug('skipping entry without title')
                ignored += 1
                continue

            # Set the title from the source field
            entry.title = entry[title_field]

            # Check we haven't already processed this entry in a previous run
            if last_entry_id == entry.title + entry.get('guid', ''):
                log.verbose('Not processing entries from last run.')
                # Let details plugin know that it is ok if this task doesn't produce any entries
                task.no_entries_ok = True
                break

            # convert title to ascii (cleanup)
            if config.get('ascii', False):
                entry.title = entry.title.encode('ascii', 'ignore')

            # remove annoying zero width spaces
            entry.title = entry.title.replace(u'\u200B', u'')

            # Dict with fields to grab mapping from rss field name to FlexGet field name
            fields = {'guid': 'guid',
                      'author': 'author',
                      'description': 'description',
                      'infohash': 'torrent_info_hash'}
            # extend the dict of fields to grab with other_fields list in config
            for field_map in config.get('other_fields', []):
                fields.update(field_map)

            # helper
            # TODO: confusing? refactor into class member ...

            def add_entry(ea):
                ea['title'] = entry.title

                for rss_field, flexget_field in fields.iteritems():
                    if rss_field in entry:
                        if not isinstance(getattr(entry, rss_field), basestring):
                            # Error if this field is not a string
                            log.error('Cannot grab non text field `%s` from rss.' % rss_field)
                            # Remove field from list of fields to avoid repeated error
                            config['other_fields'].remove(rss_field)
                            continue
                        if not getattr(entry, rss_field):
                            log.debug('Not grabbing blank field %s from rss for %s.' % (rss_field, ea['title']))
                            continue
                        try:
                            ea[flexget_field] = decode_html(entry[rss_field])
                            if rss_field in config.get('other_fields', []):
                                # Print a debug message for custom added fields
                                log.debug('Field `%s` set to `%s` for `%s`' % (rss_field, ea[rss_field], ea['title']))
                        except UnicodeDecodeError:
                            log.warning('Failed to decode entry `%s` field `%s`' % (ea['title'], rss_field))
                # Also grab pubdate if available
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    ea['rss_pubdate'] = datetime(*entry.published_parsed[:6])
                # store basic auth info
                if 'username' in config and 'password' in config:
                    ea['basic_auth_username'] = config['username']
                    ea['basic_auth_password'] = config['password']
                entries.append(ea)

            # create from enclosures if present
            enclosures = entry.get('enclosures', [])

            if len(enclosures) > 1 and not config.get('group_links'):
                # There is more than 1 enclosure, create an Entry for each of them
                log.debug('adding %i entries from enclosures' % len(enclosures))
                for enclosure in enclosures:
                    if not 'href' in enclosure:
                        log.debug('RSS-entry `%s` enclosure does not have URL' % entry.title)
                        continue
                    # There is a valid url for this enclosure, create an Entry for it
                    ee = Entry()
                    self.add_enclosure_info(ee, enclosure, config.get('filename', True), True)
                    add_entry(ee)
                # If we created entries for enclosures, we should not create an Entry for the main rss item
                continue

            # create flexget entry
            e = Entry()

            if not isinstance(config.get('link'), list):
                # If the link field is not a list, search for first valid url
                if config['link'] == 'auto':
                    # Auto mode, check for a single enclosure url first
                    if len(entry.get('enclosures', [])) == 1 and entry['enclosures'][0].get('href'):
                        self.add_enclosure_info(e, entry['enclosures'][0], config.get('filename', True))
                    else:
                        # If there is no enclosure url, check link, then guid field for urls
                        for field in ['link', 'guid']:
                            if entry.get(field):
                                e['url'] = entry[field]
                                break
                else:
                    if entry.get(config['link']):
                        e['url'] = entry[config['link']]
            else:
                # If link was passed as a list, we create a list of urls
                for field in config['link']:
                    if entry.get(field):
                        e.setdefault('url', entry[field])
                        if entry[field] not in e.setdefault('urls', []):
                            e['urls'].append(entry[field])

            if config.get('group_links'):
                # Append a list of urls from enclosures to the urls field if group_links is enabled
                e.setdefault('urls', [e['url']]).extend(
                    [enc.href for enc in entry.get('enclosures', []) if enc.get('href') not in e['urls']])

            if not e.get('url'):
                log.debug('%s does not have link (%s) or enclosure' % (entry.title, config['link']))
                ignored += 1
                continue

            add_entry(e)

        # Save last spot in rss
        if rss.entries:
            log.debug('Saving location in rss feed.')
            task.simple_persistence['%s_last_entry' % url_hash] = rss.entries[0].title + rss.entries[0].get('guid', '')

        if ignored:
            if not config.get('silent'):
                log.warning('Skipped %s RSS-entries without required information (title, link or enclosures)' % ignored)

        return entries

register_plugin(InputRSS, 'rss', api_ver=2)
