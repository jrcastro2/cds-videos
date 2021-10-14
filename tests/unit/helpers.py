# -*- coding: utf-8 -*-
#
# This file is part of CDS.
# Copyright (C) 2016, 2017, 2020 CERN.
#
# CDS is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# CDS is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with CDS; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status
# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""Helper functions for usage in tests."""

from __future__ import absolute_import, print_function

import copy
import json
import os
from time import sleep
from os.path import join

import mock
import pkg_resources
import six
from celery import shared_task, states
from flask_security import login_user, current_user
from invenio_accounts.models import User
from invenio_db import db
from invenio_files_rest.models import ObjectVersion, ObjectVersionTag
from invenio_indexer.api import RecordIndexer
from invenio_pidstore import current_pidstore
from invenio_pidstore.providers.recordid import RecordIdProvider
from invenio_records import Record
from six import BytesIO
from sqlalchemy.orm.attributes import flag_modified

from cds.modules.deposit.api import Project, Video
from cds.modules.flows.serializers import FlowResponseSerializer
from cds.modules.flows.task_api import Task
from cds.modules.records.api import Category, Keyword
from cds.modules.records.minters import catid_minter
from cds.modules.flows.tasks import (
    AVCTask, TranscodeVideoTask, ExtractMetadataTask, update_record
)
from cds.modules.flows.api import Flow
from cds.modules.flows.models import Status

import random
import uuid
from shutil import copyfile

from flask import current_app
from cds_sorenson.api import _get_quality_preset
from cds_sorenson.error import SorensonError


@shared_task(bind=True)
def failing_task(self, *args, **kwargs):
    """A failing shared task."""
    self.update_state(state=states.FAILURE, meta={})


@shared_task(bind=True)
def success_task(self, *args, **kwargs):
    """A failing shared task."""
    self.update_state(state=states.SUCCESS, meta={})


class sse_failing_task(AVCTask):
    """Failing celery tasks with sse channel."""

    def __init__(self):
        """init."""
        self._type = 'simple_failure'
        self._base_payload = {}

    def run(self, *args, **kwargs):
        """A failing shared task."""
        pass

    def on_success(self, exc, task_id, *args, **kwargs):
        """Set Fail."""
        self.on_failure(exc, task_id, args, kwargs, '')


class sse_success_task(AVCTask):

    def __init__(self):
        self._type = 'simple_failure'
        self._base_payload = {}

    def run(self, *args, **kwargs):
        """A failing shared task."""
        pass


@shared_task
def simple_add(x, y):
    """Simple shared task."""
    return x + y


class sse_simple_add(AVCTask):
    def __init__(self):
        self._type = 'simple_add'
        self._base_payload = {}

    def run(self, x, y, **kwargs):
        """Simple shared task."""
        self._base_payload = {"deposit_id": kwargs.get('deposit_id')}
        return x + y


MOCK_TASK_NAMES = {
    'helpers.sse_simple_add': 'sse_simple_add',
    'helpers.sse_success_task': 'sse_success_task',
    'helpers.sse_failing_task': 'sse_failing_task',
}


def create_category(api_app, db, data):
    """Create a fixture for category."""
    with db.session.begin_nested():
        record_id = uuid.uuid4()
        catid_minter(record_id, data)
        category = Category.create(data)

    db.session.commit()

    indexer = RecordIndexer()
    indexer.index_by_id(category.id)

    return category


def create_keyword(data):
    """Create a fixture for keyword."""
    with db.session.begin_nested():
        keyword = Keyword.create(data)

    db.session.commit()

    indexer = RecordIndexer()
    indexer.index_by_id(keyword.id)

    return keyword


def create_record(data):
    """Create a test record."""
    with db.session.begin_nested():
        data = copy.deepcopy(data)
        rec_uuid = uuid.uuid4()
        pid = current_pidstore.minters['cds_recid'](rec_uuid, data)
        record = Record.create(data, id_=rec_uuid)
    return pid, record


def get_json(response):
    """Get JSON from response."""
    return json.loads(response.get_data(as_text=True))


def assert_hits_len(res, hit_length):
    """Assert number of hits."""
    assert res.status_code == 200
    assert len(get_json(res)['hits']['hits']) == hit_length


def mock_current_user(*args2, **kwargs2):
    """Mock current user not logged-in."""
    return None


def transcode_task(bucket, filesize, filename, preset_qualities):
    """Get a transcode task."""
    obj = ObjectVersion.create(bucket, key=filename,
                               stream=BytesIO(b'\x00' * filesize))
    ObjectVersionTag.create(obj, 'display_aspect_ratio', '16:9')
    obj_id = str(obj.version_id)
    db.session.commit()

    return (obj_id, [
        TranscodeVideoTask().s(
            version_id=obj_id, preset_quality=preset_quality, sleep_time=0)
        for preset_quality in preset_qualities
    ])


def get_presets_applied():
    """Return list of preset applied."""
    presets = current_app.config['CDS_SORENSON_PRESETS']['16:9']
    return {key: preset for (key, preset) in presets.items()
            if preset['width'] <= 640}


def get_object_count(download=True, frames=True, transcode=True):
    """Get number of ObjectVersions, based on executed tasks."""
    return sum([
        # Master file
        1 if download else 0,
        # 10 frames + 1 GIF
        11 if frames else 0,
        # count the presets with width x height < 640x320 (video resolution)
        (len(get_presets_applied())) if transcode else 0,
    ])


def get_tag_count(download=True, metadata=True, frames=True, transcode=True,
                  is_local=False):
    """Get number of ObjectVersionTags, based on executed tasks."""
    # download: _flow_id, uri_origin, context_type, media_type, preview
    tags_download = 5
    if is_local:
        # uri_origin doesn't exists if not downloaded
        tags_download = tags_download - 1

    # metadata
    tags_extract_metadata = len(ExtractMetadataTask.format_keys) + \
                            len(ExtractMetadataTask.stream_keys)

    # transcode
    # number of keys inside object `tags` (basically, the number of tags)
    tags_keys = 14

    return sum([
        tags_download if download else 0,
        tags_extract_metadata if download and metadata else 0,
        ((10 * 4) + 3) if frames else 0,
        # count the presets with width x height < 640x320 (video resolution)
        (len(get_presets_applied())) * tags_keys if transcode else 0,
    ])


def new_project(app, es, cds_jsonresolver, users, location, db,
                deposit_metadata, project_data=None, wait=None):
    """New project with videos."""
    project_data = project_data or {
        'title': {
            'title': 'my project',
        },
        'description': 'in tempor reprehenderit enim eiusmod',
    }
    project_data.update(deposit_metadata)
    project_video_1 = {
        'title': {
            'title': '&lt;b&gt;<i>video 1</i>&lt;/b&gt;',
        },
        'description': 'in tempor reprehenderit enim eiusmod &lt;b&gt;<i>html'
                       '</i>&lt;/b&gt;',
        'featured': True,
        'vr': True,
        'language': 'en',
        'date': '2017-09-25',
    }
    project_video_1.update(deposit_metadata)
    project_video_2 = {
        'title': {
            'title': 'video 2',
        },
        'description': 'in tempor reprehenderit enim eiusmod',
        'featured': False,
        'vr': False,
        'language': 'en',
        'date': '2017-09-25',
    }
    project_video_2.update(deposit_metadata)
    indexer = RecordIndexer()
    with app.test_request_context():
        login_user(User.query.get(users[0]))

        # create empty project
        project = Project.create(project_data).commit()

        # create videos
        project_video_1['_project_id'] = project['_deposit']['id']
        project_video_2['_project_id'] = project['_deposit']['id']
        video_1 = Video.create(project_video_1)
        video_2 = Video.create(project_video_2)

        # save project and video
        project.commit()
        video_1.commit()
        video_2.commit()

    db.session.commit()
    indexer.index(project)
    indexer.index(video_1)
    indexer.index(video_2)
    if wait is not False:
        sleep(2)
    return project, video_1, video_2


def get_indexed_records_from_mock(mock_indexer):
    """Get indexed records from mock."""
    indexed = []
    for call in mock_indexer.call_args_list:
        ((arg,), _) = call
        if isinstance(arg, six.string_types):
            indexed.append(arg)
        else:
            indexed.extend(arg)
    return indexed


@mock.patch('cds.modules.records.providers.CDSRecordIdProvider.create',
            RecordIdProvider.create)
def prepare_videos_for_publish(videos, with_files=False):
    """Prepare video for publishing (i.e. fill extracted metadata)."""
    metadata_dict = dict(
        bit_rate='679886',
        duration='60.140000',
        size='5111048',
        avg_frame_rate='288000/12019',
        codec_name='h264',
        codec_long_name='H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10',
        width='640',
        height='360',
        nb_frames='1440',
        display_aspect_ratio='16:9',
        color_range='tv',
    )
    for video in videos:
        # Inline update
        if '_cds' not in video:
            video['_cds'] = {}
        video['_cds']['extracted_metadata'] = metadata_dict
        if with_files:
            video['_files'] = get_files_metadata(video.files.bucket.id)

        # DB update
        update_record(
            recid=video.id,
            patch=[{
                'op': 'add',
                'path': '/_cds/extracted_metadata',
                'value': metadata_dict,
            }],
            validator='cds.modules.records.validators.PartialDraft4Validator'
        )


def get_files_metadata(bucket_id):
    """Return _files data filled with a valid version id."""
    bucket_id = str(bucket_id)
    object_version = ObjectVersion.create(bucket_id, 'test_object_version',
                                          stream=BytesIO(b'\x00' * 200))
    object_version_id = str(object_version.version_id)
    db.session.commit()
    return [
        dict(
            bucket_id=bucket_id,
            context_type='master',
            media_type='video',
            content_type='mp4',
            checksum=rand_md5(),
            completed=True,
            key='test.mp4',
            frame=[
                dict(
                    bucket_id=bucket_id,
                    checksum=rand_md5(),
                    completed=True,
                    key='frame-{}.jpg'.format(i),
                    links=dict(self='/api/files/...'),
                    progress=100,
                    size=123456,
                    tags=dict(
                        master=object_version_id,
                        type='frame',
                        timestamp=(float(i) / 10) * 60.095
                    ),
                    version_id=object_version_id)
                for i in range(11)
            ],
            tags=dict(
                bit_rate='11915822',
                width='4096',
                height='2160',
                uri_origin='https://test_domain.ch/test.mp4',
                duration='60.095', ),
            subformat=[
                dict(
                    bucket_id=bucket_id,
                    context_type='subformat',
                    media_type='video',
                    content_type='mp4',
                    checksum=rand_md5(),
                    completed=True,
                    key='test_{}'.format(i),
                    links=dict(self='/api/files/...'),
                    progress=100,
                    size=123456,
                    tags=dict(
                        _sorenson_job_id=rand_version_id(),
                        master=object_version_id,
                        preset_quality='240p',
                        width=1000,
                        height=1000,
                        video_bitrate=123456, ),
                    version_id=object_version_id, )
                for i in range(5)
            ],
            playlist=[
                dict(
                    bucket_id=bucket_id,
                    context_type='playlist',
                    media_type='text',
                    content_type='smil',
                    checksum=rand_md5(),
                    completed=True,
                    key='test.smil',
                    links=dict(
                        self='/api/files/...'),
                    progress=100,
                    size=123456,
                    tags=dict(master=object_version_id),
                    version_id=object_version_id, )
            ],
        )
    ]


def add_video_tags(video_object):
    """Add standard technical metadata tags to a video."""
    ObjectVersionTag.create(video_object, 'duration', '60.095000')
    ObjectVersionTag.create(video_object, 'width', '640')
    ObjectVersionTag.create(video_object, 'height', '360')
    ObjectVersionTag.create(video_object, 'display_aspect_ratio', '16:9')


#
# Random generation
#
def rand_md5():
    return 'md5:{:032d}'.format(random.randint(1, 1000000))


def rand_version_id():
    return str(uuid.uuid4())


def endpoint_get_schema(path):
    """Get schema for jsonschemas."""
    with open(pkg_resources.resource_filename(
            'cds_dojson.schemas', path), 'r') as f:
        return json.load(f)


def check_deposit_record_files(deposit, deposit_expected, record,
                               record_expected):
    """Check deposit and record files expected."""
    # check deposit
    deposit_objs = [obj.key for obj in ObjectVersion.query_heads_by_bucket(
        deposit.files.bucket).all()]
    assert sorted(deposit_expected) == sorted(deposit_objs)
    assert deposit.files.bucket.locked is False
    # check record
    record_objs = [obj.key for obj in ObjectVersion.query_heads_by_bucket(
        record.files.bucket).all()]
    assert sorted(record_expected) == sorted(record_objs)
    assert record.files.bucket.locked is True


def load_json(datadir, filename):
    """Load file in json format."""
    filepath = join(datadir, filename)
    data = None
    with open(filepath, 'r') as file_:
        data = json.load(file_)
    return data


def reset_oauth2():
    """After a OAuth2 request, reset user."""
    if hasattr(current_user, 'login_via_oauth2'):
        del current_user.login_via_oauth2


def get_local_file(bucket, datadir, filename):
    """Create local file as objectversion."""
    stream = open(join(datadir, filename), 'rb')
    object_version = ObjectVersion.create(
        bucket, "test.mp4", stream=stream)
    version_id = object_version.version_id
    db.session.commit()
    return version_id


def get_frames(*args, **kwargs):
    """list of frames of a master video."""
    return [{'key': 'frame-{0}.jpg'.format(index),
             'tags': {'context_type': 'frame', 'content_type': 'jpg',
                      'media_type': 'image'},
             'tags_to_transform': {'timestamp': (index * 10) - 5},
             'filepath': '/path/to/file'} for index in range(1, 11)]


def get_migration_streams(datadir):
    """Get migration files streams."""

    def migration_streams(*args, **kwargs):
        if kwargs['file_']['tags']['media_type'] == 'video':
            path = join(datadir, 'test.mp4')
        elif kwargs['file_']['tags']['media_type'] == 'image':
            if kwargs['file_']['tags']['context_type'] == 'frame':
                path = join(datadir, kwargs['file_']['key'])
            else:
                path = join(datadir, 'frame-1.jpg')
        return open(path, 'rb'), os.path.getsize(path)

    return migration_streams


class MockSorenson(object):
    """Mock class to use for local testing."""

    def __init__(self):
        self.jobs = {}

    def start_encoding(
            self,
            input_file,
            output_file,
            desired_quality,
            display_aspect_ratio,
            max_height=None,
            max_width=None,
            **kwargs
    ):
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = {
            'step': 0,
            'input_file': input_file,
            'output_file': output_file,
            'quality': desired_quality,
            'aspect_ratio': display_aspect_ratio,
        }
        aspect_ratio, preset_config = _get_quality_preset(
            desired_quality,
            display_aspect_ratio,
            video_height=max_height,
            video_width=max_width
        )
        return job_id, aspect_ratio, preset_config

    def stop_encoding(self, job_id):
        try:
            del self.jobs[job_id]
        except KeyError:
            raise SorensonError('No status found for job: {0}'.format(job_id))

    def get_encoding_status(self, job_id):
        try:
            job = self.jobs[job_id]
        except KeyError:
            return 'Cancelled', 100

        if job['step'] < 5:
            fail_value = random.random()
            # Simulate random failures 5% of the times
            if fail_value >= 0.99:  # Change value to higher probability
                job_status = 6
                job_progress = 100
            else:
                job_status = job['step']
                # 5 means finished, before that it's all running
                job_progress = (job_status / 5.0) * 100
        else:
            job_status = job['step']
            job_progress = 100

        status = current_app.config['CDS_SORENSON_STATUSES'].get(job_status)
        # Increase step for next call
        if job_status == 5:
            # This means finished, copy input file to destination
            copyfile(job['input_file'], '{}.mp4'.format(job['output_file']))
        elif job_status < 5:
            job_status = job_status + 1

        job['step'] = job_status

        return status, job_progress


class MockSorensonHappy(MockSorenson):

    def get_encoding_status(self, job_id):
        status = current_app.config['CDS_SORENSON_STATUSES'].get(5)
        job_progress = 100
        return status, job_progress


class MockSorensonFailed(MockSorenson):
    def get_encoding_status(self, job_id):
        status = current_app.config['CDS_SORENSON_STATUSES'].get(6)
        job_progress = 100
        return status, job_progress


def mock_compute_status(cls, statuses):
    return Status.FAILURE


def mock_build_flow_status_json(flow_json):
    """Build serialized status object."""
    status = ([], [])
    for task in flow_json['tasks']:
        task_status = Task.build_task_json_status(task)

        # Get the UI name of the task
        task_name = task_status["name"]
        assert task_name
        # Calculate the right position inside the tuple
        step = (
            0
            if task_name in ('sse_simple_add',)
            else 1
        )

        status[step].append(task_status)

    return status


class TestFlow(Flow):

    def _workflow(self, deposit_id, user_id, bucket_id,
                  version_id, key, uri=None):
        with db.session.begin_nested():
            workflow = TestFlow.create(
                'TestFlow',
                payload={
                    'deposit_id': deposit_id,
                    'bucket_id': bucket_id,
                    'version_id': version_id,
                    'key': key,
                    'uri': uri,
                },
                user_id="1",
                deposit_id=deposit_id,
            )

        db.session.commit()

        return workflow

    def build_steps(self):
        self._tasks.append((sse_simple_add(), {'x': 1, 'y': 2}))
        self._tasks.append([
            (sse_failing_task(), {}), (sse_failing_task(), {})])

    def run(self, deposit_id, user_id, version_id, bucket_id, key,
            uri=None):
        flow = self._workflow(
            deposit_id=deposit_id,
            user_id=user_id,
            bucket_id=bucket_id,
            version_id=version_id,
            key=key, uri=uri,
        )
        flow_id = flow.id
        flow.assemble()
        with db.session.begin_nested():
            flow.payload['deposit_id'] = deposit_id
            flag_modified(flow.model, 'payload')
        db.session.commit()

        flow.start()
        flow = TestFlow.get_flow(flow_id)

        return flow
