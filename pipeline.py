# encoding=utf8
import datetime
from distutils.version import StrictVersion
import hashlib
import os.path
import random
from seesaw.config import realize, NumberConfigValue
from seesaw.item import ItemInterpolation, ItemValue
from seesaw.task import SimpleTask, LimitConcurrent
from seesaw.tracker import GetItemFromTracker, PrepareStatsForTracker, \
    UploadWithTracker, SendDoneToTracker
import shutil
import socket
import subprocess
import sys
import time

import seesaw
from seesaw.externalprocess import WgetDownload
from seesaw.pipeline import Pipeline
from seesaw.project import Project
from seesaw.util import find_executable


# check the seesaw version
if StrictVersion(seesaw.__version__) < StrictVersion("0.1.5"):
    raise Exception("This pipeline needs seesaw version 0.1.5 or higher.")


###########################################################################
# Find a useful Wget+Lua executable.
#
# WGET_LUA will be set to the first path that
# 1. does not crash with --version, and
# 2. prints the required version string
WGET_LUA = find_executable(
    "Wget+Lua",
    ["GNU Wget 1.14.lua.20130523-9a5c"],
    [
        "./wget-lua",
        "./wget-lua-warrior",
        "./wget-lua-local",
        "../wget-lua",
        "../../wget-lua",
        "/home/warrior/wget-lua",
        "/usr/bin/wget-lua"
    ]
)

if not WGET_LUA:
    raise Exception("No usable Wget+Lua found.")


###########################################################################
# The version number of this pipeline definition.
#
# Update this each time you make a non-cosmetic change.
# It will be added to the WARC files and reported to the tracker.
VERSION = "20140820.01"
USER_AGENT = 'ArchiveTeam'
TRACKER_ID = 'wallbase'
TRACKER_HOST = 'tracker.archiveteam.org'


###########################################################################
# This section defines project-specific tasks.
#
# Simple tasks (tasks that do not need any concurrency) are based on the
# SimpleTask class and have a process(item) method that is called for
# each item.
class CheckIP(SimpleTask):
    def __init__(self):
        SimpleTask.__init__(self, "CheckIP")
        self._counter = 0

    def process(self, item):
        # NEW for 2014! Check if we are behind firewall/proxy

        if self._counter <= 0:
            item.log_output('Checking IP address.')
            ip_set = set()

            ip_set.add(socket.gethostbyname('twitter.com'))
            ip_set.add(socket.gethostbyname('facebook.com'))
            ip_set.add(socket.gethostbyname('youtube.com'))
            ip_set.add(socket.gethostbyname('microsoft.com'))
            ip_set.add(socket.gethostbyname('icanhas.cheezburger.com'))
            ip_set.add(socket.gethostbyname('archiveteam.org'))

            if len(ip_set) != 6:
                item.log_output('Got IP addresses: {0}'.format(ip_set))
                item.log_output(
                    'Are you behind a firewall/proxy? That is a big no-no!')
                raise Exception(
                    'Are you behind a firewall/proxy? That is a big no-no!')

        # Check only occasionally
        if self._counter <= 0:
            self._counter = 10
        else:
            self._counter -= 1


class PrepareDirectories(SimpleTask):
    def __init__(self, warc_prefix):
        SimpleTask.__init__(self, "PrepareDirectories")
        self.warc_prefix = warc_prefix

    def process(self, item):
        item_name = item["item_name"]
        escaped_item_name = item_name.replace(':', '_').replace('/', '_')
        dirname = "/".join((item["data_dir"], escaped_item_name))

        if os.path.isdir(dirname):
            shutil.rmtree(dirname)

        os.makedirs(dirname)

        item["item_dir"] = dirname
        item["warc_file_base"] = "%s-%s-%s" % (self.warc_prefix, escaped_item_name,
            time.strftime("%Y%m%d-%H%M%S"))

        open("%(item_dir)s/%(warc_file_base)s.warc.gz" % item, "w").close()


class MoveFiles(SimpleTask):
    def __init__(self):
        SimpleTask.__init__(self, "MoveFiles")

    def process(self, item):
        # NEW for 2014! Check if wget was compiled with zlib support
        if os.path.exists("%(item_dir)s/%(warc_file_base)s.warc"):
            raise Exception('Please compile wget with zlib support!')

        os.rename("%(item_dir)s/%(warc_file_base)s.warc.gz" % item,
              "%(data_dir)s/%(warc_file_base)s.warc.gz" % item)

        shutil.rmtree("%(item_dir)s" % item)


def get_hash(filename):
    with open(filename, 'rb') as in_file:
        return hashlib.sha1(in_file.read()).hexdigest()


CWD = os.getcwd()
PIPELINE_SHA1 = get_hash(os.path.join(CWD, 'pipeline.py'))
LUA_SHA1 = get_hash(os.path.join(CWD, 'wallbase.lua'))


def stats_id_function(item):
    # NEW for 2014! Some accountability hashes and stats.
    d = {
        'pipeline_hash': PIPELINE_SHA1,
        'lua_hash': LUA_SHA1,
        'python_version': sys.version,
    }

    return d


class WgetArgs(object):
    def realize(self, item):
        wget_args = [
            WGET_LUA,
            "-U", USER_AGENT,
            "-nv",
            "--lua-script", "wallbase.lua",
            "-o", ItemInterpolation("%(item_dir)s/wget.log"),
            "--no-check-certificate",
            "--output-document", ItemInterpolation("%(item_dir)s/wget.tmp"),
            "--truncate-output",
            "-e", "robots=off",
            "--no-cookies",
            "--rotate-dns",
#            "--recursive", "--level=inf",
            "--no-parent",
            "--page-requisites",
            "--timeout", "30",
            "--tries", "inf",
            "--span-hosts",
            "--waitretry", "30",
            "--domains", "wallbase.cc,walb.es",
            "--warc-file", ItemInterpolation("%(item_dir)s/%(warc_file_base)s"),
            "--warc-header", "operator: Archive Team",
            "--warc-header", "wallbase-dld-script-version: " + VERSION,
            "--warc-header", ItemInterpolation("wallbase-user: %(item_name)s"),
        ]
        
        item_name = item['item_name']
        assert ':' in item_name
        item_type, item_value = item_name.split(':', 1)
        
        item['item_type'] = item_type
        item['item_value'] = item_value
        
        assert item_type in ('wallpaper', 'tag', 'user', 'collection', 'color', 'toplist', 'screenshot', 'favorite')
        
        if item_type == 'wallpaper':
            #example url: http://wallbase.cc/wallpaper/2940947
            #example item: wallpaper:2940947
            wget_args.append('http://wallbase.cc/wallpaper/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/index.php/wallpaper/index/{0}'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/high-resolution/wallpaper-{0}.jpg'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/manga-anime/wallpaper-{0}.jpg'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/rozne/wallpaper-{0}.jpg'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/high-resolution/wallpaper-{0}.png'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/manga-anime/wallpaper-{0}.png'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/rozne/wallpaper-{0}.png'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/high-resolution/wallpaper-{0}.gif'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/manga-anime/wallpaper-{0}.gif'.format(item_value))
            wget_args.append('http://wallpapers.wallbase.cc/rozne/wallpaper-{0}.gif'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/go/{0}/next'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/go/{0}/prev'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/go/{0}/next/'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/go/{0}/prev/'.format(item_value))
            wget_args.append('http://origthumbs.wallbase.cc//rozne/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://origthumbs.wallbase.cc//high-resolution/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://origthumbs.wallbase.cc//manga-anime/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://thumbs.wallbase.cc//rozne/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://thumbs.wallbase.cc//high-resolution/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://thumbs.wallbase.cc//manga-anime/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://sthumbs.wallbase.cc//rozne/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://sthumbs.wallbase.cc//high-resolution/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://sthumbs.wallbase.cc//manga-anime/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://origthumbs.wallbase.cc/rozne/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://origthumbs.wallbase.cc/high-resolution/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://origthumbs.wallbase.cc/manga-anime/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://thumbs.wallbase.cc/rozne/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://thumbs.wallbase.cc/high-resolution/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://thumbs.wallbase.cc/manga-anime/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://sthumbs.wallbase.cc/rozne/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://sthumbs.wallbase.cc/high-resolution/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://sthumbs.wallbase.cc/manga-anime/thumb-{0}.jpg'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/similar/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/add_copyright/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/load_grouped_walls/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/purity/{0}/2'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/purity/{0}/1'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/purity/{0}/0'.format(item_value))
            wget_args.append('http://wallbase.cc/index.php/wallpaper/delete/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/add2favorites/{0}/0'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/add2favorites/{0}/1'.format(item_value))
            wget_args.append('http://wallbase.cc/wallpaper/delete/{0}/rep'.format(item_value))
            wget_args.append('http://walb.es/{0}'.format(item_value))
        elif item_type == 'tag':
            #example url: http://wallbase.cc/search?tag=8179
            #example item: tag:8179:fate/stay night
            if ':' in item_value:
                item_num, item_name = item_value.split(':', 1)
                item['item_num'] = item_num
                item['item_name'] = item_name
                wget_args.append('http://wallbase.cc/search?tag={0}'.format(item_num))
                wget_args.append('http://wallbase.cc/search/index/?tag={0}'.format(item_num))
                wget_args.append('http://wallbase.cc/search/index/0?tag={0}'.format(item_num))
                wget_args.append('http://wallbase.cc/search/index/60?tag={0}'.format(item_num))
                wget_args.append('http://wallbase.cc/tags/{0}'.format(item_num))
                wget_args.append('http://wallbase.cc/tags/{0}/'.format(item_num))
                wget_args.append('http://wallbase.cc/tags/info/{0}'.format(item_num))
                wget_args.append('http://wallbase.cc/tags/subscribe/{0}/1'.format(item_num))
                wget_args.append('http://wallbase.cc/tags/subscribe/{0}/0'.format(item_num))
                wget_args.append('http://wallbase.cc/search?q==({0})'.format(item_name))
                wget_args.append('http://wallbase.cc/search?q==({0})&color=&section=wallpapers&q==({0})&res_opt=eqeq&res=0x0&order_mode=desc&thpp=60&purity=111&board=213&aspect=0.00'.format(item_name))
                if '/' in item_name:
                    item_value.replace('/', ' ')
                    wget_args.append('http://wallbase.cc/search?q==({0})'.format(item_name))
                    wget_args.append('http://wallbase.cc/search?q==({0})&color=&section=wallpapers&q==({0})&res_opt=eqeq&res=0x0&order_mode=desc&thpp=60&purity=111&board=213&aspect=0.00'.format(item_name))
            else:
                wget_args.append('http://wallbase.cc/search?tag={0}'.format(item_value))
                wget_args.append('http://wallbase.cc/search/index/?tag={0}'.format(item_value))
                wget_args.append('http://wallbase.cc/search/index/0?tag={0}'.format(item_value))
                wget_args.append('http://wallbase.cc/search/index/60?tag={0}'.format(item_value))
                wget_args.append('http://wallbase.cc/tags/{0}'.format(item_value))
                wget_args.append('http://wallbase.cc/tags/{0}/'.format(item_value))
                wget_args.append('http://wallbase.cc/tags/info/{0}'.format(item_value))
                wget_args.append('http://wallbase.cc/tags/subscribe/{0}/1'.format(item_value))
                wget_args.append('http://wallbase.cc/tags/subscribe/{0}/0'.format(item_value))
        elif item_type == 'user':
            #example url: http://wallbase.cc/user/id-2
            #example item: user:2
            wget_args.append('http://wallbase.cc/user/id-{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/user/id-{0}/'.format(item_value))
            wget_args.append('http://wallbase.cc/user/subscribe/{0}/1'.format(item_value))
            wget_args.append('http://wallbase.cc/user/subscribe/{0}/0'.format(item_value))
            wget_args.append('http://wallbase.cc/user/id-{0}/favorites'.format(item_value))
            wget_args.append('http://wallbase.cc/user/id-{0}/uploads'.format(item_value))
            wget_args.append('http://wallbase.cc/images/avatars/av_{0}.gif'.format(item_value))
            wget_args.append('http://wallbase.cc/images/avatars/av_{0}.png'.format(item_value))
            wget_args.append('http://wallbase.cc/images/avatars/av_{0}.jpg'.format(item_value))
        elif item_type == 'collection':
            #example url: http://wallbase.cc/collection/26215
            #example item: collection:26215
            wget_args.append('http://wallbase.cc/collection/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/collection/{0}/'.format(item_value))
            wget_args.append('http://wallbase.cc/collection/rate_coll/{0}/down'.format(item_value))
            wget_args.append('http://wallbase.cc/collection/rate_coll/{0}/up'.format(item_value))
        elif item_type == 'color':
            #example url: http://wallbase.cc/search?color=69413a
            #example item: color:69413a
            wget_args.append('http://wallbase.cc/search?color={0}'.format(item_value))
            wget_args.append('http://wallbase.cc/search?q=&section=wallpapers&board=12&res_opt=eqeq&res=0x0&aspect=0&purity=100&order=def_relevance&order_mode=desc&thpp=32&r=145&g=150&b=181&color={0}'.format(item_value))
        elif item_type == 'toplist':
            #example url: http://wallbase.cc/toplist?ts=1w
            #example item: toplist:1w
            wget_args.append('http://wallbase.cc/toplist?ts={0}'.format(item_value))
            wget_args.append('http://wallbase.cc/toplist?section=wallpapers&board=12&res_opt=eqeq&res=0x0&aspect=0&purity=100&thpp=32&ts={0}'.format(item_value))
            wget_args.append('http://wallbase.cc/toplist?section=collections&board=12&res_opt=eqeq&res=0x0&aspect=0&purity=100&thpp=32&ts={0}'.format(item_value))
        elif item_type == 'screenshot':
            #example url: http://wallbase.cc/user/screenshot/3759
            #example item: screenshot:3759
            wget_args.append('http://wallbase.cc/user/screenshot/{0}'.format(item_value))
            wget_args.append('http://slave.wallbase.cc/desktops/desk_{0}.jpg'.format(item_value))
            wget_args.append('http://slave.wallbase.cc/desktops/desk_{0}_orig.jpg'.format(item_value))
            wget_args.append('http://slave.wallbase.cc/desktops/desk_{0}_orig.jpg#-moz-resolution=16,16'.format(item_value))
            wget_args.append('http://slave.wallbase.cc/desktops/desk_{0}.png'.format(item_value))
            wget_args.append('http://slave.wallbase.cc/desktops/desk_{0}.gif'.format(item_value))
        elif item_type == 'favorite':
            #example url: http://wallbase.cc/favorites/570499
            #example item: favorite:570499
            wget_args.append('http://wallbase.cc/favorites/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/favorites/{0}/'.format(item_value))
            wget_args.append('http://wallbase.cc/favorites/change_perms/{0}/1'.format(item_value))
            wget_args.append('http://wallbase.cc/favorites/change_perms/{0}/0'.format(item_value))
            wget_args.append('http://wallbase.cc/index.php/favorites/rename_coll/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/index.php/favorites/new_coll/{0}'.format(item_value))
            wget_args.append('http://wallbase.cc/favorites/delete_coll/{0}/delall'.format(item_value))
            wget_args.append('http://wallbase.cc/favorites/delete_coll/{0}/delroot'.format(item_value))
        else:
            raise Exception('Unknown item')
        
        if 'bind_address' in globals():
            wget_args.extend(['--bind-address', globals()['bind_address']])
            print('')
            print('*** Wget will bind address at {0} ***'.format(
                globals()['bind_address']))
            print('')

        return realize(wget_args, item)

###########################################################################
# Initialize the project.
#
# This will be shown in the warrior management panel. The logo should not
# be too big. The deadline is optional.
project = Project(
    title="Wallbase",
    project_html="""
        <img class="project-logo" alt="Project logo" src="http://archiveteam.org/images/1/1a/Wall-your-base-are-belong-to-us.png" height="50px" title=""/>
        <h2>wallbase.cc <span class="links"><a href="http://wallbase.cc/">Website</a> &middot; <a href="http://tracker.archiveteam.org/wallbase/">Leaderboard</a></span></h2>
        <p>Archiving wallpapers from wallbase.cc.</p>
    """
)

pipeline = Pipeline(
    CheckIP(),
    GetItemFromTracker("http://%s/%s" % (TRACKER_HOST, TRACKER_ID), downloader,
        VERSION),
    PrepareDirectories(warc_prefix="wallbase"),
    WgetDownload(
        WgetArgs(),
        max_tries=5,
        accept_on_exit_code=[0, 8],
        env={
            "item_dir": ItemValue("item_dir"),
            "item_value": ItemValue("item_value"),
            "item_type": ItemValue("item_type"),
        }
    ),
    PrepareStatsForTracker(
        defaults={"downloader": downloader, "version": VERSION},
        file_groups={
            "data": [
                ItemInterpolation("%(item_dir)s/%(warc_file_base)s.warc.gz")
            ]
        },
        id_function=stats_id_function,
    ),
    MoveFiles(),
    LimitConcurrent(NumberConfigValue(min=1, max=4, default="1",
        name="shared:rsync_threads", title="Rsync threads",
        description="The maximum number of concurrent uploads."),
        UploadWithTracker(
            "http://%s/%s" % (TRACKER_HOST, TRACKER_ID),
            downloader=downloader,
            version=VERSION,
            files=[
                ItemInterpolation("%(data_dir)s/%(warc_file_base)s.warc.gz")
            ],
            rsync_target_source_path=ItemInterpolation("%(data_dir)s/"),
            rsync_extra_args=[
                "--recursive",
                "--partial",
                "--partial-dir", ".rsync-tmp",
            ]
            ),
    ),
    SendDoneToTracker(
        tracker_url="http://%s/%s" % (TRACKER_HOST, TRACKER_ID),
        stats=ItemValue("stats")
    )
)
