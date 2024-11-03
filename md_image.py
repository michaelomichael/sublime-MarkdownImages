import sublime
import sublime_plugin
from collections import defaultdict
import struct
import imghdr
import base64
import urllib.request
import urllib.parse
import io
import os.path
import subprocess
import sys
import re

DEBUG = False

LEADING_WHITESPACE_REGEX = re.compile("^([ \t]*)")
IMAGE_ATTRIBUTES_REGEX = re.compile(r'.*\)\{(.*)\}')

def debug(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


settings_file = 'MarkdownImages.sublime-settings'


def get_settings():
    return sublime.load_settings(settings_file)


class MarkdownImagesPlugin(sublime_plugin.EventListener):

    def on_load(self, view):
        settings = get_settings()
        show_local = settings.get('show_local_images_on_load', False)
        show_remote = settings.get('show_remote_images_on_load', False)
        if not show_local and not show_remote:
            return
        if not self._should_run_for_extension(settings, view):
            return
        self._update_images(settings,
                            view,
                            show_local=show_local,
                            show_remote=show_remote)

    def on_post_save(self, view):
        settings = get_settings()
        show_local = settings.get('show_local_images_on_post_save')
        show_remote = settings.get('show_remote_images_on_post_save')
        if not show_local and not show_remote:
            return
        if not self._should_run_for_extension(settings, view):
            return
        self._update_images(settings,
                            view,
                            show_local=show_local,
                            show_remote=show_remote)

    def on_close(self, view):
        ImageHandler.on_close(view)

    def _should_run_for_extension(self, settings, view):
        extensions = settings.get('extensions')
        fn = view.file_name()
        _, ext = os.path.splitext(fn)
        # extensions can be either a list or single string
        if isinstance(extensions, str):
            return ext == extensions
        return ext in extensions

    def _update_images(self, settings, view, **kwargs):
        max_width = settings.get('img_maxwidth', None)
        base_path = settings.get('base_path', None)
        ImageHandler.hide_images(view)
        ImageHandler.show_images(view,
                                 max_width=max_width,
                                 show_local=kwargs.get('show_local', False),
                                 show_remote=kwargs.get('show_remote', False),
                                 base_path=base_path)


class ImageHandler:
    """
    Static class to bundle image handling.
    """

    selector = 'markup.underline.link.image.markdown'
    # Maps view IDs to sets of phantom (key, html) pairs
    phantoms = defaultdict(set)
    # Cached remote URL image data. Kept even if not rendered.
    cached_remote_urls = defaultdict(dict)

    @staticmethod
    def on_close(view):
        ImageHandler._erase_phantoms(view)
        ImageHandler.cached_remote_urls.pop(view.id(), None)

    @staticmethod
    def show_images(view, max_width=None, show_local=True, show_remote=False, base_path=""):
        debug("show_images")
        if not show_local and not show_remote:
            debug("doing nothing")
            return

        # Note: Excessive size will cause the ST3 window to become blank
        # unrecoverably. 1024 apperas to be a safe limit,
        # but can possibly go higher.
        if not max_width or max_width < 0:
            max_width = 1024

        phantoms = {}
        img_regs = view.find_by_selector(ImageHandler.selector)

        # Handling space characters in image links
        # Image links not enclosed in <> that contain spaces
        # are parsed by sublime as multiple links instead of one.
        # Example: "![](my file.png)" gets parsed as two links: "my" and "file.png".
        # We detect when two links are separated only by spaces and merge them
        indexes_to_merge = []
        for i, (left_reg, right_reg) in enumerate(zip(img_regs, img_regs[1:])):
            try:
                inter_region = sublime.Region(left_reg.end(), right_reg.begin())
                if (view.substr(inter_region)).isspace():
                    # the inter_region is all spaces
                    # Noting that left and right regions must be merged
                    indexes_to_merge += [i + 1]
            except UnicodeDecodeError as e:
                print("Warning: MarkdownImages: error handling space characters in line starting at character %d: %s" % (left_reg.a, e))

        new_img_regs = []
        for i in range(len(img_regs)):
            if i in indexes_to_merge:
                new_img_regs[-1] = new_img_regs[-1].cover(img_regs[i])
            else:
                new_img_regs += [img_regs[i]]
        img_regs = new_img_regs

        for region in reversed(img_regs):
            line_region = view.line(region)
            print("## In reversed list, looking at region (%d,%d) [%s]" % (region.a, region.b, view.substr(region)))
            image_data = None
            rel_p = view.substr(region)

            # If an image link is enclosed in <> to tolerate spaces in it,
            # then the > appears at the end of rel_p for some reason.
            # This character makes the link invalid, so it must be removed
            if rel_p[-1] == '>':
                rel_p = rel_p[0:-1]

            # (Windows) cutting the drive letter from the path,
            # otherwise urlparse interprets it as a scheme (like 'file' or 'http')
            # and generates a bogus url object like:
            # url= ParseResult(scheme='c', netloc='', path='/path/image.png', params='', query='', fragment='')
            drive_letter, rel_p = os.path.splitdrive(rel_p)

            url = urllib.parse.urlparse(rel_p)
            try:
                if url.scheme and url.scheme != 'file':
                    img_src, h, w, file_type, url, image_data = ImageHandler.prepare_remote_image(rel_p,
                                                                                                                 show_remote,
                                                                                                                 url,
                                                                                                                 view)
                else:
                    img_src, h, w, file_type, url = ImageHandler.prepare_local_image(base_path,
                                                                                                    drive_letter,
                                                                                                    show_local, url,
                                                                                                    view)
            except SkipImageException:
                continue
            # PreparedImageDetails now contains :
            #       html_template, h, w, img_src, file_type, url, urldata

            if not file_type:
                debug("unknown file_type")
                continue

            try:
                img_attributes = ImageHandler.get_adjusted_img_attributes(h, w, max_width, line_region, region, view)
            except UnicodeDecodeError as e:
                print("Warning: MarkdownImages: error fetching image attributes in line starting at character %d: %s" % (region.a, e))
                continue

            # Force the phantom image view to append below the first non-whitespace character in the line.
            # Otherwise, the phantom image view interlaces in between
            # word-wrapped lines
            line = view.substr(line_region)
            whitespace = LEADING_WHITESPACE_REGEX.search(line).group(1)
            start_point = line_region.a + len(whitespace)
            key = 'mdimage-' + str(start_point)

            html = u'''
                    <a href="%s">
                        <img src="%s" class="centerImage" %s>
                    </a>
                ''' % (url.geturl(), img_src, img_attributes)

            phantom = (key, html)
            phantoms[key] = phantom
            if phantom in ImageHandler.phantoms[view.id()]:
                debug("Phantom unchanged")
                continue

            debug("Creating phantom", phantom[0])
            print("## Creating phantom. Start point is %d" % start_point)
            view.add_phantom(phantom[0],
                             sublime.Region(start_point),
                             phantom[1],
                             sublime.LAYOUT_BELOW,
                             ImageHandler.on_navigate)
            ImageHandler.phantoms[view.id()].add(phantom)
            if image_data is not None:
                ImageHandler.cached_remote_urls[view.id()][rel_p] = image_data

        # Erase leftover phantoms
        for p in list(ImageHandler.phantoms[view.id()]):
            if phantoms.get(p[0]) != p:
                view.erase_phantoms(p[0])
                ImageHandler.phantoms[view.id()].remove(p)

        if not ImageHandler.phantoms[view.id()]:
            ImageHandler.phantoms.pop(view.id(), None)

    @staticmethod
    def get_adjusted_img_attributes(h, w, max_width, line_region, region, view):
        """
        Use everything within the "{...}", if the user provided it, e.g. for:
            [Screenshot](Test.png){width="373" height="310"}
        we would return:
            width="373" height="310"

        If no attributes are provided, we'll calculate our own width and height
        attributes based on the detected image size.
        """
        # TODO -- handle custom sizes better
        # If only width or height are provided, scale the other dimension
        # properly
        # Width defined in custom size should override max_width
        img_attributes = get_provided_img_attributes(view, line_region, region)
        if not img_attributes and w > 0 and h > 0:
            if max_width and w > max_width:
                m = max_width / w
                h *= m
                w = max_width
            img_attributes = 'width="{}" height="{}"'.format(w, h)
        return img_attributes

    @staticmethod
    def prepare_local_image(base_path, drive_letter, show_local, url, view):
        if not show_local:
            raise SkipImageException()

        # Convert relative paths to be relative to the current file
        # or project folder.
        # NOTE: if the current file or project folder can't be
        # determined (e.g. if the view content is not in a project and
        # hasn't been saved), then it will anchor to /.
        file_path = url.path

        # Force paths to be prefixed with base_path if it was provided
        # in settings.
        if base_path:
            file_path = os.path.join(base_path, file_path)
        if not os.path.isabs(file_path):
            folder = get_path_for(view)
            file_path = os.path.join(folder, file_path)
        file_path = os.path.normpath(file_path)

        # (Windows) Adding back the drive letter that was cut from the path before
        file_path = drive_letter + file_path
        url = url._replace(scheme='file', path=file_path)

        try:
            w, h, file_type = get_file_image_size(file_path)
        except Exception as e:
            msg = "MarkdownImages: Failed to load [%s]" % file_path
            debug(msg, e)
            raise Exception(msg) from e

        img_src = urllib.parse.urlunparse(url)

        # On Windows, urlunparse adds a third slash after 'file://' for some reason
        # This breaks the image url, so it must be removed
        # splitdrive() detects windows because it only returns something if the
        # path contains a drive letter
        if os.path.splitdrive(file_path)[0]:
            img_src = img_src.replace('file:///', 'file://', 1)

        return img_src, h, w, file_type, url

    @staticmethod
    def prepare_remote_image(rel_p, show_remote, url, view):
        if not show_remote:
            raise SkipImageException()

        # We can't render SVG images, so skip the request
        # Note: not all URLs that return SVG end with .svg
        # We could use a HEAD request to check the Content-Type before
        # downloading the image, but the size of an SVG is typically
        # so small to not be worth the extra request
        if url.path.endswith('.svg'):
            print("MarkdownImages: We can't render SVG images yet, sorry.")
            raise SkipImageException()

        debug("image url", rel_p)
        image_data = ImageHandler.cached_remote_urls[view.id()].get(rel_p)
        if not image_data:
            try:
                response = urllib.request.urlopen(rel_p)
            except Exception as e:
                msg = "MarkdownImages: Failed to open URL [%s]" % rel_p
                debug(msg, e)
                raise Exception(msg) from e

            try:
                image_data = response.read()
            except Exception as e:
                msg = "MarkdownImages: Failed to read data from URL [%s]" % rel_p
                debug(msg, e)
                raise Exception(msg) from e
        try:
            w, h, file_type = get_image_size(io.BytesIO(image_data))
        except Exception as e:
            msg = "MarkdownImages: Failed to get_image_size for data from URL [%s]" % rel_p
            debug(msg, e)
            raise Exception(msg) from e

        b64_data = base64.encodestring(image_data).decode('ascii').replace('\n', '')
        img_src = "data:image/%s;base64,%s" % (file_type, b64_data)
        return img_src, h, w, file_type, url, image_data

    @staticmethod
    def on_navigate(url):
        print("MarkdownImages: Opening URL [%s]" % url)
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.call([opener, url])

    @staticmethod
    def hide_images(view):
        ImageHandler._erase_phantoms(view)

    @staticmethod
    def _erase_phantoms(view):
        for p in ImageHandler.phantoms[view.id()]:
            view.erase_phantoms(p[0])
        ImageHandler.phantoms.pop(view.id(), None)
        # Cached URL data is kept


def get_provided_img_attributes(view, line_region, link_region=None):
    # find attrs for this link
    full_line = view.substr(line_region)
    link_till_eol = full_line[link_region.a - line_region.a:]
    # find attr if present
    print("## Attrs is [%s]" % link_till_eol)
    m = re.match(IMAGE_ATTRIBUTES_REGEX, link_till_eol)
    if m:
        return m.groups()[0]
    return ''


def get_file_image_size(file_path):
    with open(file_path, 'rb') as f:
        return get_image_size(f)


def get_image_size(f):
    """
    Determine the image type of img and return its size.
    """
    head = f.read(24)
    file_type = None

    debug(str(head))
    debug(str(head[:4]))
    debug(head[:4] == b'<svg')

    if imghdr.what('', head) == 'png':
        debug('detected png')
        file_type = "png"
        check = struct.unpack('>i', head[4:8])[0]
        if check != 0x0d0a1a0a:
            return None, None, file_type
        width, height = struct.unpack('>ii', head[16:24])
    elif imghdr.what('', head) == 'gif':
        debug('detected gif')
        file_type = "gif"
        width, height = struct.unpack('<HH', head[6:10])
    elif imghdr.what('', head) == 'jpeg':
        debug('detected jpeg')
        file_type = "jpeg"
        try:
            f.seek(0)  # Read 0xff next
            size = 2
            ftype = 0
            while not 0xc0 <= ftype <= 0xcf:
                f.seek(size, 1)
                byte = f.read(1)
                while ord(byte) == 0xff:
                    byte = f.read(1)
                ftype = ord(byte)
                size = struct.unpack('>H', f.read(2))[0] - 2
            # SOFn block
            f.seek(1, 1)  # skip precision byte.
            height, width = struct.unpack('>HH', f.read(4))
        except Exception as e:
            debug("determining jpeg image size failed", e)
            return None, None, file_type
    elif head[:4] == b'<svg':
        debug('detected svg')
        # SVG is not rendered by ST3 in phantoms.
        # The SVG would need to be rendered as png/jpg separately, and its data
        # placed into the phantom
        return None, None, None
    else:
        debug('unable to detect image')
        return None, None, None
    return width, height, file_type


def get_path_for(view):
    """
    Returns the path of the current file in view.
    Returns / if no path is found
    """
    if view.file_name():
        return os.path.dirname(view.file_name())
    if view.window().project_file_name():
        return os.path.dirname(view.window().project_file_name())
    return '/'


class MarkdownImagesShowCommand(sublime_plugin.TextCommand):
    """
    Show local images inline.
    """

    def run(self, edit, **kwargs):
        settings = get_settings()
        max_width = settings.get('img_maxwidth', None)
        show_local = kwargs.get('show_local', True)
        show_remote = kwargs.get('show_remote', False)
        base_path = settings.get('base_path', None)
        ImageHandler.show_images(self.view,
                                 show_local=show_local,
                                 show_remote=show_remote,
                                 max_width=max_width,
                                 base_path=base_path)


class MarkdownImagesHideCommand(sublime_plugin.TextCommand):
    """
    Hide all shown images.
    """

    def run(self, edit):
        ImageHandler.hide_images(self.view)


class SkipImageException(Exception):
    pass
