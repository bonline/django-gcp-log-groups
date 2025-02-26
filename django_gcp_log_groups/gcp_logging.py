import datetime
import logging
import time
import random
import os
import google.cloud.logging as gcplogging
from django.conf import settings

from .background_thread import BackgroundThreadTransport


LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)
PROJECT = os.environ.get("GROUPED_LOGGING_GCP_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
CLIENT = gcplogging.Client(project=PROJECT)


REMOTE_IP_HEADER = os.environ.get("GROUPED_LOGGING_REMOTE_IP_HEADER")
if REMOTE_IP_HEADER:
    REMOTE_IP_HEADER = 'HTTP_' + REMOTE_IP_HEADER.replace('-', '_').upper()
else:
    REMOTE_IP_HEADER = "REMOTE_ADDR"

LOG_PREFIX = os.environ.get("GROUPED_LOGGING_LOG_PREFIX")
TRANSPORT_PARENT = None
if hasattr(settings, "GCP_LOG_USE_X_HTTP_CLOUD_CONTEXT"):
    USE_X_HTTP_CLOUD_CONTEXT = settings.GCP_LOG_USE_X_HTTP_CLOUD_CONTEXT or False
else:
    USE_X_HTTP_CLOUD_CONTEXT = False

if not USE_X_HTTP_CLOUD_CONTEXT:
    PARENT_LOG_NAME = f"{LOG_PREFIX}_request_log" if LOG_PREFIX else "request_log"
    TRANSPORT_PARENT = BackgroundThreadTransport(CLIENT, PARENT_LOG_NAME)

CHILD_LOG_NAME = f"{LOG_PREFIX}_application" if LOG_PREFIX else "application"
TRANSPORT_CHILD = BackgroundThreadTransport(CLIENT, CHILD_LOG_NAME)

if os.environ.get("K_SERVICE"):
    RESOURCE = gcplogging.Resource(
        type='cloud_run_revision',
        labels={
            "service_name": os.environ.get("K_SERVICE"),
            "revision_name": os.environ.get("K_REVISION"),
            "configuration_name": os.environ.get("K_CONFIGURATION"),
        }
    )
else:
    RESOURCE = gcplogging.Resource(type='gae_app', labels={})
LABELS = None
MLOGLEVELS = []


class GCPHandler(logging.Handler):

    def __init__(self, trace, span):
        logging.Handler.__init__(self)
        self.trace = trace
        self.span = span

    def emit(self, record):
        msg = self.format(record)
        SEVERITY = record.levelname

        # if the current log is at a lower level than is setup, skip it
        if getattr(logging, record.levelname) < LOGGER.level:
            return
        MLOGLEVELS.append(SEVERITY)

        TRANSPORT_CHILD.send(
            msg,
            timestamp=datetime.datetime.utcnow(),
            severity=SEVERITY,
            resource=RESOURCE,
            labels=LABELS,
            trace=self.trace,
            span_id=self.span)


class GCPLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        span = None
        trace = None
        if USE_X_HTTP_CLOUD_CONTEXT:
            trace = request.META.get("HTTP_X_CLOUD_TRACE_CONTEXT")
        if trace:
            # trace can be formatted as "X-Cloud-Trace-Context:
            # trace/SPAN_ID;o=TRACE_TRUE"
            rawTrace = trace.split('/')
            trace = rawTrace[0]
            if len(rawTrace) > 1:
                span = rawTrace[1].split(';')[0]
        else:
            chars = "abcdefghijklmnopqrstuvwxyz1234567890"
            trace = "".join([random.choice(chars) for x in range(0, 32)])
        trace = f"projects/{PROJECT}/traces/{trace}"
        start_time = time.time()

        # Add logging handler for this request
        gcp_handler = GCPHandler(trace=trace, span=span)
        gcp_handler.setLevel(logging.DEBUG)
        LOGGER.addHandler(gcp_handler)

        response = self.get_response(request)

        self.make_parent_log(trace, span, request, start_time, response)

        # Remove logging handler for this request
        LOGGER.removeHandler(gcp_handler)
        return response

    def make_parent_log(self, trace, span, request, start_time, response):
        # https://github.com/googleapis/googleapis/ ...
        # blob/master/google/logging/type/http_request.proto
        REQUEST = {
            'requestMethod': request.method,
            'requestUrl': request.get_full_path(),
            'requestSize': request.META.get("CONTENT_LENGTH") or 0,
            'remoteIp': request.META.get(REMOTE_IP_HEADER, 'unknown'),
            'status': response.status_code,
            'responseSize': response.get("Content-Length") or 0,
            'latency': "%.5fs" % (time.time() - start_time),
        }

        if request.META.get("HTTP_USER_AGENT"):
            REQUEST['userAgent'] = request.META["HTTP_USER_AGENT"]

        if request.META.get("HTTP_REFERER"):
            REQUEST['referer'] = request.META["HTTP_REFERER"]

        # find the log level priority sub-messages; apply the max
        # level to the root log message
        if len(MLOGLEVELS) == 0:
            severity = logging.getLevelName(logging.INFO)
            if response.status_code >= 500:
                severity = logging.getLevelName(logging.ERROR)
            elif response.status_code >= 400:
                severity = logging.getLevelName(logging.WARNING)
        else:
            severity = min(MLOGLEVELS)

        del MLOGLEVELS[:]

        if not USE_X_HTTP_CLOUD_CONTEXT:
            TRANSPORT_PARENT.send(
                None,
                timestamp=datetime.datetime.utcnow(),
                severity=severity,
                resource=RESOURCE,
                labels=LABELS,
                trace=trace,
                span_id=span,
                http_request=REQUEST,
            )
