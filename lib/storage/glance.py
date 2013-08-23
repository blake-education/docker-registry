
import os

import glanceclient

from signals import tag_created, tag_deleted
from . import Storage
from .s3 import S3Storage
from .local import LocalStorage


class GlanceStorage(object):

    """ This class is a dispatcher, it forwards methods accessing repositories
        to the alternate storage defined in the config and it forwards methods
        accessing images to the GlanceStorageLayers class below.
    """

    def __init__(self, config):
        self._config = config
        self._storage_layers = GlanceStorageLayers(config)
        kind = config.get('storage_alternate', 'local')
        self._storage_base = Storage()
        if kind == 's3':
            self._storage_tags = S3Storage(config)
        elif kind == 'local':
            self._storage_tags = LocalStorage(config)
        else:
            raise ValueError('Not supported storage \'{0}\''.format(kind))

    def _resolve_class_path(self, method_name, *args, **kwargs):
        path = ''
        if 'path' in kwargs:
            path = kwargs['path']
        elif len(args) > 0 and isinstance(args[0], basestring):
            path = args[0]
        if path.startswith(Storage.images):
            obj = self._storage_layers
        elif path.startswith(Storage.repositories):
            obj = self._storage_tags
        else:
            obj = self._storage_base
        if not hasattr(obj, method_name):
            return
        return getattr(obj, method_name)

    def __getattr__(self, name):
        def dispatcher(*args, **kwargs):
            attr = self._resolve_class_path(name, *args, **kwargs)
            if not attr:
                raise ValueError('Cannot dispath method '
                                 '"{0}" args: {1}, {2}'.format(name,
                                                               args,
                                                               kwargs))
            if callable(attr):
                return attr(*args, **kwargs)
            return attr
        return dispatcher


class GlanceStorageLayers(Storage):

    """ This class stores the image layers into OpenStack Glance.
        However tags are still stored on other filesystem-like stores.
    """

    #FIXME(sam): set correct diskformat and container format
    disk_format = 'raw'
    container_format = 'bare'

    def __init__(self, config):
        self._config = config
        # Hooks the tag changes
        tag_created.connect(self._handler_tag_created)
        tag_deleted.connect(self._handler_tag_deleted)

    def _create_glance_client(self):
        #FIXME(sam) the token is taken from the environ for testing only!
        endpoint = self._config.glance_endpoint
        return glanceclient.Client('1', endpoint=endpoint,
                                   token=os.environ['OS_AUTH_TOKEN'])

    def _init_path(self, path, create=True):
        """ This resolve a standard Docker Registry path
            and returns: glance_image_id, property_name
            If property name is None, we're want to reach the image_data
        """
        parts = path.split('/')
        if len(parts) != 3 or parts[0] != self.images:
            raise ValueError('Invalid path: {0}'.format(path))
        image_id = parts[1]
        filename = parts[2]
        glance = self._create_glance_client()
        image = self._find_image_by_id(glance, image_id)
        if not image and create is True:
            image = glance.images.create(
                disk_format=self.disk_format,
                container_format=self.container_format,
                properties={'id': image_id})
        propname = 'meta_{0}'.format(filename)
        if filename == 'layer':
            propname = None
        return image, propname

    def _find_image_by_id(self, glance, image_id):
        filters = {
            'disk_format': self.disk_format,
            'container_format': self.container_format,
            'properties': {'id': image_id}
        }
        images = [i for i in glance.images.list(filters=filters)]
        if images:
            return images[0]

    def _clear_images_name(self, glance, image_name):
        images = glance.images.list(filters={'name': image_name})
        for image in images:
            image.update(name=None, purge_props=False)

    def _handler_tag_created(self, sender, namespace, repository, tag, value):
        glance = self._create_glance_client()
        image = self._find_image_by_id(glance, value)
        if not image:
            # No corresponding image, ignoring
            return
        image_name = '{0}:{1}'.format(repository, tag)
        if namespace != 'library':
            image_name = '{0}/{1}'.format(namespace, image_name)
        # Clear any previous image tagged with this name
        self._clear_images_name(glance, image_name)
        image.update(name=image_name, purge_props=False)

    def _handler_tag_deleted(self, sender, namespace, repository, tag):
        image_name = '{0}:{1}'.format(repository, tag)
        if namespace != 'library':
            image_name = '{0}/{1}'.format(namespace, image_name)
        glance = self._create_glance_client()
        self._clear_images_name(glance, image_name)

    def get_content(self, path):
        (image, propname) = self._init_path(path, False)
        if not propname:
            raise ValueError('Wrong call (should be stream_read)')
        if not image or propname not in image.properties:
            raise IOError('No such image {0}'.format(path))
        return image.properties[propname]

    def put_content(self, path, content):
        (image, propname) = self._init_path(path)
        if not propname:
            raise ValueError('Wrong call (should be stream_write)')
        props = {propname: content}
        image.update(properties=props, purge_props=False)

    def stream_read(self, path):
        (image, propname) = self._init_path(path, False)
        if propname:
            raise ValueError('Wrong call (should be get_content)')
        if not image:
            raise IOError('No such image {0}'.format(path))
        return image.data(do_checksum=False)

    def stream_write(self, path, fp):
        (image, propname) = self._init_path(path)
        if propname:
            raise ValueError('Wrong call (should be put_content)')
        image.update(data=fp, purge_props=False)

    def exists(self, path):
        (image, propname) = self._init_path(path, False)
        if not image:
            return False
        if not propname:
            return True
        return (propname in image.properties)

    def remove(self, path):
        (image, propname) = self._init_path(path, False)
        if not image:
            return
        if propname:
            # Delete only the image property
            props = image.properties
            if propname in props:
                del props[propname]
                image.update(properties=props)
            return
        image.delete()

    def get_size(self, path):
        (image, propname) = self._init_path(path, False)
        if not image:
            raise OSError('No such image: \'{0}\''.format(path))
        return image.size
