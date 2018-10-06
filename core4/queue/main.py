"""
This module implements general methods of the core4 worker queue used for
example by :mod:`core4.queue.worker` and :mod:`core4.queue.process`.
"""

import importlib
import sys
import traceback

import pymongo.errors
from datetime import timedelta

import core4.error
import core4.service.setup
import core4.util
from core4.base import CoreBase
from core4.queue.job import STATE_PENDING

STATE_WAITING = (core4.queue.job.STATE_DEFERRED,
                 core4.queue.job.STATE_FAILED)
STATE_STOPPED = (core4.queue.job.STATE_KILLED,
                 core4.queue.job.STATE_INACTIVE,
                 core4.queue.job.STATE_ERROR)


class CoreQueue(CoreBase, metaclass=core4.util.Singleton):

    def enqueue(self, cls=None, name=None, by=None, **kwargs):
        """
        Enqueues the passed job identified by it's :meth:`.qual_name`. The job
        is represented by a document in MongoDB collection ``sys.queue``.

        :param kwargs: dict
        :return: enqueued job object
        """
        core4.service.setup.CoreSetup().make_queue()
        job = self.job_factory(name or cls, **kwargs)
        # update job properties
        job.__dict__["attempts_left"] = job.__dict__["attempts"]
        job.__dict__["state"] = STATE_PENDING
        enqueued_from = {
            "at": lambda: core4.util.mongo_now(),
            "hostname": lambda: core4.util.get_hostname(),
            "parent_id": lambda: None,
            "username": lambda: core4.util.get_username()
        }
        if by is None:
            by = {}
        job.__dict__["enqueued"] = {}
        for k in ("at", "hostname", "parent_id", "username"):
            job.__dict__["enqueued"][k] = by.get(k, enqueued_from[k]())
        # save
        doc = job.serialise()
        try:
            ret = self.config.sys.queue.insert_one(doc)
        except pymongo.errors.DuplicateKeyError as exc:
            raise core4.error.CoreJobExists(
                "job [{}] exists with args {}".format(
                    job.qual_name(), job.args))
        job.__dict__["_id"] = ret.inserted_id
        job.__dict__["identifier"] = ret.inserted_id
        self.logger.info(
            'successfully enqueued [%s] with [%s]', job.qual_name(), job._id)
        return job

    def job_factory(self, job, **kwargs):
        """
        Takes the fully qualified job name, identifies and imports the job class
        and returns the job class.

        :param job_name: fully qualified name of the job
        :return: job class
        """
        if isinstance(job, str):
            parts = job.split(".")
            package = ".".join(parts[:-1])
            if not package:
                raise core4.error.CoreJobNotFound(
                    "[{}] not found".format(job))
            class_name = parts[-1]
            module = importlib.import_module(package)
            cls = getattr(module, class_name)
        else:
            cls = job
        if not isinstance(cls, type):
            raise TypeError(
                "{} not a class".format(repr(job)))
        obj = cls(**kwargs)
        if not isinstance(obj, core4.queue.job.CoreJob):
            raise TypeError(
                "{} not a subclass of CoreJob".format(repr(job)))
        obj.validate()
        return obj

    def enter_maintenance(self):
        """
        Enters global maintenance mode. This mode is indicated with a document
        ``_id == "__maintenance__"`` in collection ``sys.worker``.

        :return: ``True`` if maintenance mode is set, else ``False``
        """
        ret = self.config.sys.worker.update_one(
            {"_id": "__maintenance__"},
            update={
                "$set": {
                    "timestamp": core4.util.now()
                }
            },
            upsert=True
        )
        return ret.raw_result["n"] == 1

    def leave_maintenance(self):
        """
        Leaves global maintenance mode. See :meth:`.enter_maintenance`.

        :return: ``True`` if maintenance mode has been left
        """
        ret = self.config.sys.worker.delete_one({"_id": "__maintenance__"})
        return ret.raw_result["n"] == 1

    def maintenance(self):
        """
        :return: ``True`` if core4 is in global maintenance mode, else
                 ``False``
        """
        return self.config.sys.worker.count({"_id": "__maintenance__"}) > 0

    def halt(self, at=None, now=None):
        """
        This method has two operating modes. With the ``at`` parameter it tests
        if a core4 *halt* request is set. With a passed ``now=True`` the method
        requests core4 to halt operations.

        .. note:: Either the ``at`` or the ``now`` parameter must be set.

        :param at: :class:`datetime.datetime` to compare
        :param now: bool
        :return: ``True`` if core4 is halted, else ``False``
        """
        if now is not None:
            ret = self.config.sys.worker.update_one(
                {"_id": "__halt__"},
                update={"$set": {"timestamp": core4.util.now()}},
                upsert=True)
            return ret.raw_result["n"] == 1
        elif at is not None:
            return self.config.sys.worker.count(
                {"_id": "__halt__", "timestamp": {"$gte": at}}) > 0
        else:
            raise core4.error.Core4UsageError(".halt requires now or past")

    def remove_job(self, _id):
        """
        Requests to remove the job with the passed ``_id`` from ``sys.queue``.

        :param _id: :class:`bson.object.ObjectId`
        :return: ``True`` if the request succeeded, else ``False``
        """
        at = core4.util.now()
        ret = self.config.sys.queue.update_one(
            {
                "_id": _id,
                "removed_at": None
            },
            update={
                "$set": {
                    "removed_at": at
                }
            }
        )
        if ret.raw_result["n"] == 1:
            self.logger.warning(
                "flagged job [%s] to be remove at [%s]", _id, at)
            return True
        self.logger.error("failed to flag job [%s] to be remove", _id)
        return False

    def restart_job(self, _id):
        """
        Requests to restart the job with the passed ``_id``.

        .. note:: Jobs in waiting state (``deferred`` and ``failed``) are
                  restarted by resetting their ``query_at`` attribute. Jobs in
                  state ``stopped`` (``killed``, ``inactive`` and ``error``)
                  are restarted by journaling the existing job and enqueuing
                  an identical job into ``sys.queue``. These jobs have a new
                  job ``_id``. Additionally the newly created job carries a
                  ``enqueued.parent_id`` attribute refering the journaled job.
                  Vice versa the parent job got a ``enqueued.child_id``
                  attribute linking the newly created job.

        :param _id: :class:`bson.object.ObjectId`
        :return: ``True`` if the request succeeded, else ``False``
        """
        if self._restart_waiting(_id):
            self.logger.warning('successfully restarted [%s]', _id)
            return _id
        else:
            new_id = self._restart_stopped(_id)
            if new_id:
                self.logger.warning('successfully restarted [%s] '
                                    'with [%s]', _id, new_id)
                return new_id
            else:
                self.logger.error("failed to restart [%s]", _id)
                return _id

    def _restart_waiting(self, _id):
        # internal method used by .restart_job
        ret = self.config.sys.queue.update_one(
            {
                "_id": _id,
                "state": {
                    "$in": STATE_WAITING
                }
            },
            update={
                "$set": {
                    "query_at": None
                }
            }
        )
        return ret.raw_result["n"] == 1

    def _restart_stopped(self, _id):
        # internal method used by .restart_job
        job = self.find_job(_id)
        if job.state in STATE_STOPPED:
            if self.lock_job('__user__', _id):
                ret = self.config.sys.queue.delete_one({"_id": _id})
                if ret.raw_result["n"] == 1:
                    enqueue = job.enqueued
                    enqueue["parent_id"] = job._id
                    enqueue["at"] = core4.util.mongo_now()
                    doc = dict([(k, v) for k, v in job.serialise().items() if
                                k in core4.queue.job.ENQUEUE_ARGS])
                    new_job = self.enqueue(name=job.qual_name(), by=enqueue,
                                           **doc)
                    job.enqueued["child_id"] = new_job._id
                    self.journal(job.serialise())
                    return new_job._id
        return None

    def kill_job(self, _id):
        """
        Requests to kill the job with the passed ``_id``.

        .. note:: Only jobs in state ``running`` can be killed.

        :param _id: :class:`bson.object.ObjectId`
        :return: ``True`` if the request succeeded, else ``False``
        """
        at = core4.util.now()
        ret = self.config.sys.queue.update_one(
            {
                "_id": _id,
                "killed_at": None,
                "state": core4.queue.job.STATE_RUNNING
            },
            update={
                "$set": {
                    "killed_at": at
                }
            }
        )
        if ret.raw_result["n"] == 1:
            self.logger.warning(
                "flagged job [%s] to be killed at [%s]", _id, at)
            return True
        self.logger.error("failed to flag job [%s] to be killed", _id)
        return False

    def lock_job(self, identifier, _id):
        """
        Reserve the job for exclusive processing. This method utililses
        collection ``sys.lock``.

        :param identifier: to assign to the reservation
        :param _id: job ``_id``
        :return: ``True`` if reservation succeeded, else ``False``
        """
        try:
            self.config.sys.lock.insert_one({"_id": _id, "worker": identifier})
            self.logger.debug('successfully reserved [%s]', _id)
            return True
        except pymongo.errors.DuplicateKeyError:
            self.logger.debug('failed to reserve [%s]', _id)
        except:
            raise
        return False

    def unlock_job(self, _id):
        """
        Release/unlock the job from ``sys.lock``.

        :param _id: :class:`bson.object.ObjectId`
        :return: ``True`` if the job has been successfully released, else
                 ``False``
        """
        ret = self.config.sys.lock.delete_one({"_id": _id})
        if ret.raw_result["n"] == 1:
            self.logger.debug('successfully released [%s]', _id)
            return True
        self.logger.error('failed to release [%s]', _id)
        return False

    def journal(self, doc):
        """
        Insert the passed MongoDB document into collection ``sys.journal``

        :param doc: dict (MongoDB document)
        :return: ``True`` if the document has been inserted, else ``False``
        """
        ret = self.config.sys.journal.insert_one(doc)
        return ret.inserted_id == doc["_id"]

    def _find_job(self, _id, collection):
        # internal method used by .load_job and .find_job
        doc = collection.find_one({"_id": _id})
        if doc is None:
            raise core4.error.CoreJobNotFound(
                "job [{}] not found".format(_id))
        job = self.job_factory(doc["name"]).deserialise(**doc)
        return job

    def load_job(self, _id):
        """
        Queries the job with the passed ``_id`` from collection ``sys.queue``.
        This method retrieves operating jobs, see also :meth:`.find_job`.

        :param _id: job ``_id``
        :return: :class:`.CoreJob` object
        """
        return self._find_job(_id, self.config.sys.queue)

    def find_job(self, _id):
        """
        Queries the job with the passed ``_id`` from collection ``sys.queue``
        or ``sys.journal``. This method retrieves all known jobs including
        operating jobs from ``sys.queue`` as well as ceased jobs from
        ``sys.journal``.

        :param _id: job ``_id``
        :return: :class:`.CoreJob` object
        """
        try:
            return self.load_job(_id)
        except core4.error.CoreJobNotFound:
            return self._find_job(_id, self.config.sys.journal)
        except:
            raise

    def _finish(self, job, state):
        # internal method used to set the most relevant job attributes
        job.__dict__["state"] = state
        job.__dict__["finished_at"] = core4.util.mongo_now()
        runtime = (job.finished_at - job.started_at).total_seconds()
        job.__dict__["runtime"] = (job.runtime or 0.) + runtime
        job.__dict__["locked"] = None
        return runtime

    def _update_job(self, job, *args):
        # internal method used to update the most relevant and passed
        #   job attributes
        ret = self.config.sys.queue.update_one(
            filter={"_id": job._id},
            update={"$set": dict([(k, getattr(job, k)) for k in args])})
        if ret.raw_result["n"] != 1:
            raise RuntimeError(
                "failed to update job [{}] state [%s]".format(
                    job._id, job.state))

    def _add_exception(self, job, exception=None):
        # internal method used to add exception information to .last_error
        if exception:
            job.__dict__["last_error"] = exception
        else:
            exc_info = sys.exc_info()
            job.__dict__["last_error"] = {
                "exception": repr(exc_info[1]),
                "timestamp": core4.util.mongo_now(),
                "traceback": traceback.format_exception(*exc_info)
            }

    def set_complete(self, job):
        """
        Set the passed ``job`` to state ``complete`` and move the job from
        ``sys.queue`` to ``sys.journal``.

        This process updates the job ``state``, ``finished_at`` timestamp, the
        ``runtime``, increases the number of ``trial``s and resets the
        ``locked`` property.

        Finally, the job lock is removed from ``sys.lock``

        :param job: :class:`.CoreJob` object
        """
        self.logger.debug(
            "updating job [%s] to [%s]", job._id,
            core4.queue.job.STATE_COMPLETE)
        runtime = self._finish(job, core4.queue.job.STATE_COMPLETE)
        self._update_job(job, "state", "finished_at", "runtime", "locked",
                         "trial")
        self.logger.debug("journaling job [%s]", job._id)
        job = self.load_job(job._id)
        ret = self.config.sys.journal.insert_one(job.serialise())
        if ret.inserted_id is None:
            raise RuntimeError(
                "failed to journal job [{}]".format(job._id))
        self.logger.debug("removing completed job [%s]", job._id)
        ret = self.config.sys.queue.delete_one(filter={"_id": job._id})
        if ret.deleted_count != 1:
            raise RuntimeError(
                "failed to remove job [{}] from queue".format(job._id))
        self.unlock_job(job._id)
        job.logger.info("done execution with [complete] "
                        "after [%d] sec.", runtime)

    def set_defer(self, job):
        """
        Set the passed ``job`` to state ``deferred`` and updates the next
        query timestamp (``query_at``) according to the job's ``defer_time``.

        The defer message is appended to the job's ``last_error`` attribute.

        The method updates the job ``state``, ``finished_at`` timestamp, the
        ``runtime``, increases the number of ``trial``s, the exception message
        in ``last_error``, the ``query_at`` timestamp and resets the ``locked``
        property.

        Finally, the job lock is removed from ``sys.lock``

        :param job: :class:`.CoreJob` object
        """
        state = core4.queue.job.STATE_DEFERRED
        now = core4.util.mongo_now()
        job.__dict__["query_at"] = now + timedelta(seconds=job.defer_time)
        self.logger.debug("updating job [%s] to [%s]", job._id, state)
        runtime = self._finish(job, state)
        self._add_exception(job)
        self._update_job(job, "state", "finished_at", "runtime", "locked",
                         "last_error", "query_at", "trial")
        self.unlock_job(job._id)
        job.logger.info("done execution with [deferred] "
                        "after [%d] sec. and [%s] to go: %s", runtime,
                        job.inactive_at - now, job.last_error["exception"])

    def set_inactivate(self, job):
        """
        Set the passed ``job`` to state ``inactive``.

        The method updates the job ``state``, ``finished_at`` timestamp, the
        ``runtime``, increases the number of ``trial``s, and resets the
        ``locked`` property.

        Finally, the job lock is removed from ``sys.lock``

        :param job: :class:`.CoreJob` object
        """
        self.logger.debug("inactivate job [%s]", job._id)
        runtime = self._finish(job, core4.queue.job.STATE_INACTIVE)
        self._update_job(job, "state", "finished_at", "runtime", "locked",
                         "trial")
        self.unlock_job(job._id)
        job.logger.error("done execution with [inactive] "
                         "after [%d] sec. and [%d] trials in [%s]", runtime,
                         job.trial, job.inactive_at - job.enqueued["at"])

    def set_failed(self, job, exception=None):
        """
        If the passed job has ``.attempts_left``, then set the job state to
        ``failed`` and update the next ``query_at`` timestamp using the job
        property ``error_time``. If not, then set the job state to ``error``.

        The method additionally updates the job ``state``, ``finished_at``
        timestamp, the ``runtime``, increases the number of ``trial``s, and
        resets the  ``locked`` property. Furthermore, the ``last_error``
        property carries exception information.

        Finally, the job lock is removed from ``sys.lock``

        :param job: :class:`.CoreJob` object
        """
        if job.attempts_left > 0:
            state = core4.queue.job.STATE_FAILED
            job.__dict__["query_at"] = (core4.util.mongo_now()
                                        + timedelta(seconds=job.error_time))
        else:
            state = core4.queue.job.STATE_ERROR
        self.logger.debug("updating job [%s] to [%s]", job._id, state)
        runtime = self._finish(job, state)
        self._add_exception(job, exception)
        self._update_job(job, "state", "finished_at", "runtime", "locked",
                         "last_error", "attempts_left", "query_at", "trial")
        self.unlock_job(job._id)
        job.logger.error("done execution with [%s] "
                         "after [%d] sec. and [%d] attempts to go: %s\n%s",
                         state, runtime, job.attempts_left,
                         job.last_error["exception"],
                         "\n".join(job.last_error["traceback"]))

    def set_killed(self, job):
        """
        Set the passed ``job`` to state ``killed``.

        The method updates the job ``state``, ``finished_at``
        timestamp, the ``runtime``, increases the number of ``trial``s, and
        resets the  ``locked`` property. Furthermore, the ``last_error``
        property carries a ``JobKilledByWorker`` flag.

        Finally, the job lock is removed from ``sys.lock``

        :param job: :class:`.CoreJob` object
        """
        self.logger.debug(
            "updating job [%s] to [%s]", job._id,
            core4.queue.job.STATE_KILLED)
        runtime = self._finish(job, core4.queue.job.STATE_KILLED)
        job.__dict__["last_error"] = {
            "exception": "JobKilledByWorker",
            "timestamp": core4.util.mongo_now(),
            "traceback": None
        }
        self._update_job(job, "state", "runtime", "locked",
                         "trial", "last_error")
        self.unlock_job(job._id)
        job.logger.error("done execution with [%s] after [%d] sec.",
                         job.state, runtime)