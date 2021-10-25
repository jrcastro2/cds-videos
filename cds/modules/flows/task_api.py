"""Invenio-Flow python API."""
import json
import logging

from celery import Task as CeleryTask
from celery import current_app as celery_app
from celery.result import AsyncResult
from celery.task.control import revoke
from invenio_db import db

from .models import Status
from .models import Task as TaskModel
from .models import as_task

logger = logging.getLogger('invenio-flow')


class Task(CeleryTask):
    """The task class which is used as the minimal unit of work.

    This class is a wrapper around ``celery.Task``
    """

    def commit_status(self, task_id, state=Status.PENDING, message=''):
        """Commit task status to the database."""
        with celery_app.flask_app.app_context():
            task = TaskModel.get(task_id)
            task.status = state
            task.message = message
            db.session.merge(task)
            db.session.commit()

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Update task status on database."""
        task_id = kwargs.get('task_id', task_id)
        self.commit_status(task_id, Status.FAILURE, str(einfo))
        super(Task, self).on_failure(exc, task_id, args, kwargs, einfo)

    def on_success(self, retval, task_id, args, kwargs):
        """Update tasks status on database."""
        task_id = kwargs.get('task_id', task_id)
        self.commit_status(
            task_id,
            Status.SUCCESS,
            '{}'.format(retval),
        )
        super(Task, self).on_success(retval, task_id, args, kwargs)

    def get_status(self, task_id):
        """Get singular task status."""
        try:
            task = as_task(task_id)
        except Exception:
            raise KeyError('Task ID %s not in flow %s', task_id, self.id)

        return {'status': str(task.status), 'message': task.message}

    def stop_task(self, task_id):
        """Stop singular task."""
        try:
            task = as_task(task_id)
        except Exception:
            raise KeyError('Task ID %s not in flow %s', task_id, self.id)

        if task.status == Status.PENDING:
            revoke(str(task.id), terminate=True, signal='SIGKILL')
            result = AsyncResult(str(task.id))
            result.forget()

    @staticmethod
    def restart_task(task_id, flow_id, flow_payload):
        """Restart singular task."""
        try:
            task = as_task(task_id)
        except Exception:
            raise KeyError('Task ID %s not in flow %s', task_id, flow_id)

        # self.stop_task(task)
        # If a task gets send to the queue with the same id, it gets
        # automagically restarted, no need to stop it.

        task.status = Status.PENDING
        db.session.add(task)

        kwargs = {'flow_id': flow_id, 'task_id': str(task.id)}
        kwargs.update(task.payload)
        kwargs.update(flow_payload)
        return (
            celery_app.tasks.get(task.name).subtask(
                task_id=str(task.id),
                kwargs=kwargs,
                immutable=True,
            ).apply_async()
        )
