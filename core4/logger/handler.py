import datetime
import logging.config
import time
import traceback

import core4.error
import core4.util


def make_record(record):
    ts = time.gmtime(record.created)
    doc = {
        "created": datetime.datetime(ts.tm_year, ts.tm_mon, ts.tm_mday,
                                     ts.tm_hour, ts.tm_min, ts.tm_sec),
        "username": core4.util.get_username(),
        "hostname": core4.util.get_hostname(),
        "identifier": record.obj.identifier,
        "qual_name": record.obj.qual_name(),
        "_id": record._id,
        "msg": record.msg,
        "level": record.levelname
    }
    if record.exc_info or record.exc_text:
        doc["exception"] = {
            "info": repr(record.exc_info[1]),
            "text": traceback.format_exception(*record.exc_info)
        }
    return doc


class MongoLoggingHandler(logging.Handler):
    """
    This class implements mongo logging.
    """

    def __init__(self, connection):
        """
        Connects the logging handler with the passed MongoDB connection.

        :param connection: :class:`pymongo.collection.Collection` object
        """
        super(MongoLoggingHandler, self).__init__()
        self._collection = connection

    def handle(self, record):
        """
        Handles the logging record by translating it into a mongo database
        document. The following attributes are saved for each
        :class:`logging.LogRecord`.

        * ``_id`` - the document id as created by
          :class:`core4.logger.MongoAdapter`
        * ``created`` - the logging datetime in UTC
        * ``level`` - the log level as string (``DEBUG``, ``INFO``, ``WARNING``,
          ``ERROR``, ``CRITICAL``)
        * ``hostname`` - the name of the host sending the message
        * ``user`` - the name of the user sending
        * ``qual_name`` - the qualified name of the class as created by
          :meth:`.CoreBase.qual_name`
        * ``identifier`` - the core object identifier; this identifier qualifies
          core workers with the worker name, core jobs with the worker and
          job id and API requests with its request id
        * ``message`` - the log message

        :param record: the log record (:class:`logging.LogRecord`)
        """
        doc = make_record(record)
        self._collection.insert_one(doc)
