# Copyright 2014 OpenStack Foundation
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

import collections
import copy

from oslo.config import cfg

from glance.common import exception
from glance.common import utils
import glance.domain.proxy
from glance.openstack.common import excutils
import glance.openstack.common.log as logging
from glance import store

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class ImageRepoProxy(glance.domain.proxy.Repo):

    def __init__(self, image_repo, context, store_api):
        self.context = context
        self.store_api = store_api
        proxy_kwargs = {'context': context, 'store_api': store_api}
        super(ImageRepoProxy, self).__init__(image_repo,
                                             item_proxy_class=ImageProxy,
                                             item_proxy_kwargs=proxy_kwargs)

    def _set_acls(self, image):
        public = image.visibility == 'public'
        member_ids = []
        if image.locations and not public:
            member_repo = image.get_member_repo()
            member_ids = [m.member_id for m in member_repo.list()]
        for location in image.locations:
            self.store_api.set_acls(self.context, location['url'], public,
                                    read_tenants=member_ids)

    def add(self, image):
        result = super(ImageRepoProxy, self).add(image)
        self._set_acls(image)
        return result

    def save(self, image):
        result = super(ImageRepoProxy, self).save(image)
        self._set_acls(image)
        return result


def _check_location_uri(context, store_api, uri):
    """
    Check if an image location uri is valid.

    :param context: Glance request context
    :param store_api: store API module
    :param uri: location's uri string
    """
    is_ok = True
    try:
        size = store_api.get_size_from_backend(context, uri)
        # NOTE(zhiyan): Some stores return zero when it catch exception
        is_ok = size > 0
    except (exception.UnknownScheme, exception.NotFound):
        is_ok = False
    if not is_ok:
        raise exception.BadStoreUri(_('Invalid location: %s') % uri)


def _check_image_location(context, store_api, location):
    _check_location_uri(context, store_api, location['url'])
    store_api.check_location_metadata(location['metadata'])


def _set_image_size(context, image, locations):
    if not image.size:
        for location in locations:
            size_from_backend = glance.store.get_size_from_backend(
                context, location['url'])
            if size_from_backend:
                # NOTE(flwang): This assumes all locations have the same size
                image.size = size_from_backend
                break


class ImageFactoryProxy(glance.domain.proxy.ImageFactory):
    def __init__(self, factory, context, store_api):
        self.context = context
        self.store_api = store_api
        proxy_kwargs = {'context': context, 'store_api': store_api}
        super(ImageFactoryProxy, self).__init__(factory,
                                                proxy_class=ImageProxy,
                                                proxy_kwargs=proxy_kwargs)

    def new_image(self, **kwargs):
        locations = kwargs.get('locations', [])
        for l in locations:
            _check_image_location(self.context, self.store_api, l)

            if locations.count(l) > 1:
                raise exception.DuplicateLocation(location=l['url'])

        return super(ImageFactoryProxy, self).new_image(**kwargs)


class StoreLocations(collections.MutableSequence):
    """
    The proxy for store location property. It takes responsibility for:
    1. Location uri correctness checking when adding a new location.
    2. Remove the image data from the store when a location is removed
       from an image.
    """
    def __init__(self, image_proxy, value):
        self.image_proxy = image_proxy
        if isinstance(value, list):
            self.value = value
        else:
            self.value = list(value)

    def append(self, location):
        # NOTE(flaper87): Insert this
        # location at the very end of
        # the value list.
        self.insert(len(self.value), location)

    def extend(self, other):
        if isinstance(other, StoreLocations):
            locations = other.value
        else:
            locations = list(other)

        for location in locations:
            self.append(location)

    def insert(self, i, location):
        _check_image_location(self.image_proxy.context,
                              self.image_proxy.store_api, location)

        if location in self.value:
            raise exception.DuplicateLocation(location=location['url'])

        self.value.insert(i, location)
        _set_image_size(self.image_proxy.context,
                        self.image_proxy,
                        [location])

    def pop(self, i=-1):
        location = self.value.pop(i)
        try:
            store.delete_image_from_backend(self.image_proxy.context,
                                            self.image_proxy.store_api,
                                            self.image_proxy.image.image_id,
                                            location['url'])
        except Exception:
            with excutils.save_and_reraise_exception():
                self.value.insert(i, location)
        return location

    def count(self, location):
        return self.value.count(location)

    def index(self, location, *args):
        return self.value.index(location, *args)

    def remove(self, location):
        if self.count(location):
            self.pop(self.index(location))
        else:
            self.value.remove(location)

    def reverse(self):
        self.value.reverse()

    # Mutable sequence, so not hashable
    __hash__ = None

    def __getitem__(self, i):
        return self.value.__getitem__(i)

    def __setitem__(self, i, location):
        _check_image_location(self.image_proxy.context,
                              self.image_proxy.store_api, location)
        self.value.__setitem__(i, location)
        _set_image_size(self.image_proxy.context,
                        self.image_proxy,
                        [location])

    def __delitem__(self, i):
        location = None
        try:
            location = self.value.__getitem__(i)
        except Exception:
            return self.value.__delitem__(i)
        store.delete_image_from_backend(self.image_proxy.context,
                                        self.image_proxy.store_api,
                                        self.image_proxy.image.image_id,
                                        location['url'])
        self.value.__delitem__(i)

    def __delslice__(self, i, j):
        i = max(i, 0)
        j = max(j, 0)
        locations = []
        try:
            locations = self.value.__getslice__(i, j)
        except Exception:
            return self.value.__delslice__(i, j)
        for location in locations:
            store.delete_image_from_backend(self.image_proxy.context,
                                            self.image_proxy.store_api,
                                            self.image_proxy.image.image_id,
                                            location['url'])
            self.value.__delitem__(i)

    def __iadd__(self, other):
        self.extend(other)
        return self

    def __contains__(self, location):
        return location in self.value

    def __len__(self):
        return len(self.value)

    def __cast(self, other):
        if isinstance(other, StoreLocations):
            return other.value
        else:
            return other

    def __cmp__(self, other):
        return cmp(self.value, self.__cast(other))

    def __iter__(self):
        return iter(self.value)

    def __copy__(self):
        return type(self)(self.image_proxy, self.value)

    def __deepcopy__(self, memo):
        # NOTE(zhiyan): Only copy location entries, others can be reused.
        value = copy.deepcopy(self.value, memo)
        self.image_proxy.image.locations = value
        return type(self)(self.image_proxy, value)


def _locations_proxy(target, attr):
    """
    Make a location property proxy on the image object.

    :param target: the image object on which to add the proxy
    :param attr: the property proxy we want to hook
    """
    def get_attr(self):
        value = getattr(getattr(self, target), attr)
        return StoreLocations(self, value)

    def set_attr(self, value):
        if not isinstance(value, (list, StoreLocations)):
            raise exception.BadStoreUri(_('Invalid locations: %s') % value)
        ori_value = getattr(getattr(self, target), attr)
        if ori_value != value:
            # NOTE(zhiyan): Enforced locations list was previously empty list.
            if len(ori_value) > 0:
                raise exception.Invalid(_('Original locations is not empty: '
                                          '%s') % ori_value)
            # NOTE(zhiyan): Check locations are all valid.
            for location in value:
                _check_image_location(self.context, self.store_api,
                                      location)

                if value.count(location) > 1:
                    raise exception.DuplicateLocation(location=location['url'])
            _set_image_size(self.context, getattr(self, target), value)
            return setattr(getattr(self, target), attr, list(value))

    def del_attr(self):
        value = getattr(getattr(self, target), attr)
        while len(value):
            delete_image_from_backend(self.context, self.store_api,
                                      self.image.image_id, value[0]['url'])
            del value[0]
            setattr(getattr(self, target), attr, value)
        return delattr(getattr(self, target), attr)

    return property(get_attr, set_attr, del_attr)


class ImageProxy(glance.domain.proxy.Image):

    locations = _locations_proxy('image', 'locations')

    def __init__(self, image, context, store_api):
        self.image = image
        self.context = context
        self.store_api = store_api
        proxy_kwargs = {
            'context': context,
            'image': self,
            'store_api': store_api,
        }
        super(ImageProxy, self).__init__(
            image, member_repo_proxy_class=ImageMemberRepoProxy,
            member_repo_proxy_kwargs=proxy_kwargs)

    def delete(self):
        self.image.delete()
        if self.image.locations:
            for location in self.image.locations:
                self.store_api.delete_image_from_backend(self.context,
                                                         self.store_api,
                                                         self.image.image_id,
                                                         location['url'])

    def set_data(self, data, size=None):
        if size is None:
            size = 0  # NOTE(markwash): zero -> unknown size
        location, size, checksum, loc_meta = self.store_api.add_to_backend(
            self.context, CONF.default_store,
            self.image.image_id, utils.CooperativeReader(data), size)
        self.image.locations = [{'url': location, 'metadata': loc_meta}]
        self.image.size = size
        self.image.checksum = checksum
        self.image.status = 'active'

    def get_data(self):
        if not self.image.locations:
            raise exception.NotFound(_("No image data could be found"))
        err = None
        for loc in self.image.locations:
            try:
                data, size = self.store_api.get_from_backend(self.context,
                                                             loc['url'])

                return data
            except Exception as e:
                LOG.warn(_('Get image %(id)s data failed: '
                           '%(err)s.') % {'id': self.image.image_id,
                                          'err': utils.exception_to_str(e)})
                err = e
        # tried all locations
        LOG.error(_('Glance tried all locations to get data for image %s '
                    'but all have failed.') % self.image.image_id)
        raise err


class ImageMemberRepoProxy(glance.domain.proxy.Repo):
    def __init__(self, repo, image, context, store_api):
        self.repo = repo
        self.image = image
        self.context = context
        self.store_api = store_api
        super(ImageMemberRepoProxy, self).__init__(repo)

    def _set_acls(self):
        public = self.image.visibility == 'public'
        if self.image.locations and not public:
            member_ids = [m.member_id for m in self.repo.list()]
            for location in self.image.locations:
                self.store_api.set_acls(self.context, location['url'], public,
                                        read_tenants=member_ids)

    def add(self, member):
        super(ImageMemberRepoProxy, self).add(member)
        self._set_acls()

    def remove(self, member):
        super(ImageMemberRepoProxy, self).remove(member)
        self._set_acls()