# Copyright 2010 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import calendar
import time

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import encodeutils
import six

from glance.common import crypt
from glance.common import exception
from glance import context
import glance.db as db_api
from glance import i18n
import glance.registry.client.v1.api as registry

LOG = logging.getLogger(__name__)

_ = i18n._
_LI = i18n._LI
_LW = i18n._LW
_LE = i18n._LE

scrubber_opts = [
    cfg.IntOpt('scrub_time', default=0,
               help=_('The amount of time in seconds to delay before '
                      'performing a delete.')),
    cfg.BoolOpt('delayed_delete', default=False,
                help=_('Turn on/off delayed delete.')),
]

scrubber_cmd_opts = [
    cfg.IntOpt('wakeup_time', default=300,
               help=_('Loop time between checking for new '
                      'items to schedule for delete.'))
]

scrubber_cmd_cli_opts = [
    cfg.BoolOpt('daemon',
                short='D',
                default=False,
                help=_('Run as a long-running process. When not '
                       'specified (the default) run the scrub operation '
                       'once and then exits. When specified do not exit '
                       'and run scrub on wakeup_time interval as '
                       'specified in the config.'))
]

CONF = cfg.CONF
CONF.register_opts(scrubber_opts)
CONF.import_opt('metadata_encryption_key', 'glance.common.config')


class ScrubDBQueue(object):
    """Database-based image scrub queue class."""
    def __init__(self):
        self.scrub_time = CONF.scrub_time
        self.metadata_encryption_key = CONF.metadata_encryption_key
        registry.configure_registry_client()
        registry.configure_registry_admin_creds()
        self.registry = registry.get_registry_client(context.RequestContext())
        admin_tenant_name = CONF.admin_tenant_name
        admin_token = self.registry.auth_token
        self.admin_context = context.RequestContext(user=CONF.admin_user,
                                                    tenant=admin_tenant_name,
                                                    auth_token=admin_token)

    def add_location(self, image_id, location):
        """Adding image location to scrub queue.

        :param image_id: The opaque image identifier
        :param location: The opaque image location

        :retval A boolean value to indicate success or not
        """
        loc_id = location.get('id')
        if loc_id:
            db_api.get_api().image_location_delete(self.admin_context,
                                                   image_id, loc_id,
                                                   'pending_delete')
            return True
        else:
            return False

    def _get_images_page(self, marker):
        filters = {'deleted': True,
                   'is_public': 'none',
                   'status': 'pending_delete'}

        if marker:
            return self.registry.get_images_detailed(filters=filters,
                                                     marker=marker)
        else:
            return self.registry.get_images_detailed(filters=filters)

    def _get_all_images(self):
        """Generator to fetch all appropriate images, paging as needed."""

        marker = None
        while True:
            images = self._get_images_page(marker)
            if len(images) == 0:
                break
            marker = images[-1]['id']

            for image in images:
                yield image

    def get_all_locations(self):
        """Returns a list of image id and location tuple from scrub queue.

        :retval a list of image id, location id and uri tuple from scrub queue
        """
        ret = []

        for image in self._get_all_images():
            deleted_at = image.get('deleted_at')
            if not deleted_at:
                continue

            # NOTE: Strip off microseconds which may occur after the last '.,'
            # Example: 2012-07-07T19:14:34.974216
            date_str = deleted_at.rsplit('.', 1)[0].rsplit(',', 1)[0]
            delete_time = calendar.timegm(time.strptime(date_str,
                                                        "%Y-%m-%dT%H:%M:%S"))

            if delete_time + self.scrub_time > time.time():
                continue

            for loc in image['location_data']:
                if loc['status'] != 'pending_delete':
                    continue

                if self.metadata_encryption_key:
                    uri = crypt.urlsafe_encrypt(self.metadata_encryption_key,
                                                loc['url'], 64)
                else:
                    uri = loc['url']

                ret.append((image['id'], loc['id'], uri))
        return ret

    def has_image(self, image_id):
        """Returns whether the queue contains an image or not.

        :param image_id: The opaque image identifier

        :retval a boolean value to inform including or not
        """
        try:
            image = self.registry.get_image(image_id)
            return image['status'] == 'pending_delete'
        except exception.NotFound:
            return False


_db_queue = None


def get_scrub_queue():
    global _db_queue
    if not _db_queue:
        _db_queue = ScrubDBQueue()
    return _db_queue


class Daemon(object):
    def __init__(self, wakeup_time=300, threads=1000):
        LOG.info(_LI("Starting Daemon: wakeup_time=%(wakeup_time)s "
                     "threads=%(threads)s"),
                 {'wakeup_time': wakeup_time, 'threads': threads})
        self.wakeup_time = wakeup_time
        self.event = eventlet.event.Event()
        self.pool = eventlet.greenpool.GreenPool(threads)

    def start(self, application):
        self._run(application)

    def wait(self):
        try:
            self.event.wait()
        except KeyboardInterrupt:
            msg = _LI("Daemon Shutdown on KeyboardInterrupt")
            LOG.info(msg)

    def _run(self, application):
        LOG.debug("Running application")
        self.pool.spawn_n(application.run, self.pool, self.event)
        eventlet.spawn_after(self.wakeup_time, self._run, application)
        LOG.debug("Next run scheduled in %s seconds" % self.wakeup_time)


class Scrubber(object):
    def __init__(self, store_api):
        LOG.info(_LI("Initializing scrubber with configuration: %s") %
                 six.text_type({'registry_host': CONF.registry_host,
                                'registry_port': CONF.registry_port}))

        self.store_api = store_api

        registry.configure_registry_client()
        registry.configure_registry_admin_creds()
        self.registry = registry.get_registry_client(context.RequestContext())

        # Here we create a request context with credentials to support
        # delayed delete when using multi-tenant backend storage
        admin_tenant = CONF.admin_tenant_name
        auth_token = self.registry.auth_token
        self.admin_context = context.RequestContext(user=CONF.admin_user,
                                                    tenant=admin_tenant,
                                                    auth_token=auth_token)

        self.db_queue = get_scrub_queue()

    def _get_delete_jobs(self):
        try:
            records = self.db_queue.get_all_locations()
        except Exception as err:
            LOG.error(_LE("Can not get scrub jobs from queue: %s") %
                      encodeutils.exception_to_unicode(err))
            return {}

        delete_jobs = {}
        for image_id, loc_id, loc_uri in records:
            if image_id not in delete_jobs:
                delete_jobs[image_id] = []
            delete_jobs[image_id].append((image_id, loc_id, loc_uri))
        return delete_jobs

    def run(self, pool, event=None):
        delete_jobs = self._get_delete_jobs()

        if delete_jobs:
            for image_id, jobs in six.iteritems(delete_jobs):
                self._scrub_image(pool, image_id, jobs)

    def _scrub_image(self, pool, image_id, delete_jobs):
        if len(delete_jobs) == 0:
            return

        LOG.info(_LI("Scrubbing image %(id)s from %(count)d locations.") %
                 {'id': image_id, 'count': len(delete_jobs)})
        # NOTE(bourke): The starmap must be iterated to do work
        list(pool.starmap(self._delete_image_location_from_backend,
                          delete_jobs))

        image = self.registry.get_image(image_id)
        if image['status'] == 'pending_delete':
            self.registry.update_image(image_id, {'status': 'deleted'})

    def _delete_image_location_from_backend(self, image_id, loc_id, uri):
        if CONF.metadata_encryption_key:
            uri = crypt.urlsafe_decrypt(CONF.metadata_encryption_key, uri)

        try:
            LOG.debug("Deleting URI from image %s." % image_id)
            self.store_api.delete_from_backend(uri, self.admin_context)
            if loc_id != '-':
                db_api.get_api().image_location_delete(self.admin_context,
                                                       image_id,
                                                       int(loc_id),
                                                       'deleted')
            LOG.info(_LI("Image %s has been deleted.") % image_id)
        except Exception:
            LOG.warn(_LW("Unable to delete URI from image %s.") % image_id)
