#! /usr/bin/env python

import os
import sys
import json
from uuid import uuid4
import logging
import socket
from os import path
from functools import wraps
from subprocess import Popen, PIPE
import datetime

from flask import Flask, request, make_response
from flask import render_template
from flask import g
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug import secure_filename
from werkzeug.http import http_date
from werkzeug.routing import Submount, Map

from . import messages


STATIC_DIR = os.environ.get('STATIC_DIR', path.join(os.getcwd(), 'static'))
TEMPLATE_DIR = os.environ.get('TEMPLATE_DIR', path.join(os.getcwd(), 'templates'))
app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATE_DIR)

app.config['VERSION'] = os.environ.get('CURRENT_SHA', None)
app.config['X-HOSTNAME'] = os.environ.get('X_HOSTNAME', socket.gethostname())
app.config['BIND_INTERFACE'] = os.environ.get('BIND_INTERFACE', '127.0.0.1')
app.config['LOG_LEVEL'] = os.environ.get('LOG_LEVEL', 'INFO')
app.config['USER_HEADER_NAME'] = os.environ.get('USER_HEADER_NAME', 'Uid')
app.config['ROOT_STORAGE_PATH'] = os.environ.get("ROOT_STORAGE_PATH", "./storage")
app.config['CACHE_ROOT'] = os.environ.get('CACHE_ROOT', '%s/cache' % (app.config['ROOT_STORAGE_PATH']))
app.config['USE_RELOADER'] = os.environ.get('USE_RELOADER', 'True')
app.config['TEMPLATE_DIR'] = TEMPLATE_DIR
app.config['SUBMOUNT_PATH'] = os.environ.get('SUBMOUNT_PATH', None)
# this is to be a comman delimited list of sources for which events will be emitted
# this of course depends on the system supporting events for those sources
app.config['LOCAL_EVENT_SOURCES'] = frozenset(os.environ.get('LOCAL_EVENT_SOURCES', '').split(","))

app.jinja_loader = ChoiceLoader([
        FileSystemLoader("./templates"),
        FileSystemLoader("./pyserver/templates")
    ])

logging.basicConfig(
    format='%(asctime)s [%(levelname)s]: %(message)s',
    stream=sys.stderr,
    level=app.config['LOG_LEVEL']
)

process_start_time = datetime.datetime.now()

def emit_local_message(source, message=None):
    if source in app.config['LOCAL_EVENT_SOURCES']:
        wrapped = json.dumps(dict(source=source, message=message, user_token=g.user_token))
        messages.send(wrapped, messages.LOCAL_PUBLISH)

def get_storage_location(named):
    return path.abspath(path.join(app.config['ROOT_STORAGE_PATH'], named))

def remove_single_element_lists(d):
    new_dict = {}
    for key, value in list(d.items()):
        if type(value) == list and len(value) == 1:
            new_dict[key] = value[0]
        else:
            new_dict[key] = value
    return new_dict

def try_run(this):
    try:
        return this()
    except:
        return None

def convert_into_number(value):
    as_number = try_run(lambda: int(value)) or try_run(lambda: float(value))
    if as_number or as_number == 0:
        return as_number
    else:
        return value

def convert_types_in_dictionary(this_dictionary):
    into_this_dictionary = {}
    for key, value in list(this_dictionary.items()):
        if type(value) == dict:
            value = convert_types_in_dictionary(value)
        elif type(value) == list:
            value = convert_types_in_list(value)
        else:
            value = convert_into_number(value)
        into_this_dictionary[key] = value
    return into_this_dictionary

def convert_types_in_list(this_list):
    into_this_list = []
    for item in this_list:
        if type(item) == list:
            new_value = convert_types_in_list(item)
        elif type(item) == dict:
            new_value = convert_types_in_dictionary(item)
        else:
            new_value = convert_into_number(item)
        into_this_list.append(new_value)
    return into_this_list

def cache_my_response(vary_by=None, expiration_seconds=900):
    def cache_wrapper_decorator(f):
        @wraps(f)
        def cache_wrapper(*args, **kwargs):
            if not vary_by:
                cache_key = request.url
            else:
                from io import StringIO
                key_buffer = StringIO()
                key_buffer.write(request.url)
                for vary_by_this in vary_by:
                    key_buffer.write("%s" % request.values.get(vary_by_this, ''))
                cache_key = key_buffer.getvalue()
            cr = app.config['_CACHE'].get_or_return_from_cache(
                cache_key,
                expiration_seconds,
                lambda: f(*args, **kwargs),
                force_refresh = request.values.get('_reload_cache', False)
            )
            resp = app.make_response(cr[1])
            if cr[0]:
                resp.headers['Expires'] = http_date(cr[0])
            return resp
        return cache_wrapper
    return cache_wrapper_decorator

def make_my_response_json(f):
    @wraps(f)
    def view_wrapper(*args, **kwargs):
        try:
            view_return = f(*args, **kwargs)
            if type(view_return) == dict:
                return json_response(**view_return)
            elif type(view_return) == list:
                return json_response(view_return)
            elif type(view_return) == int:
                return json_response(**dict(status_code=view_return))
            elif type(view_return) == str:
                return json_response(view_return)
            elif type(view_return) == tuple:
                return json_response(view_return[0], status_code=view_return[1])
            else:
                return json_response(**{})
        except Exception as e:
            return json_response(**dict(status_code=400, description=e.description))
    return view_wrapper

def json_response(*args, **kwargs):
    """ Creates a JSON response for the given params, handling the creation a callback wrapper
        if a callback is provided, and allowing for either a string arg (expected to be JSON)
        or kwargs to be passed formatted correctly for the response.
        Also sets the Content-Type of the response to application/json
    """
    content_type = "application/json"
    # if provided, use the status code otherwise default to 200
    # we remove it so it doesn't end up in our response
    status_code = kwargs.pop('status_code', 200)

    # we're going to allow the callback to come essentially from wherever the user
    # choses to provide it, again remove it from kwargs so it doesn't end
    # up in the response
    callback = kwargs.pop('callback', None) or request.values.get('callback', None)

    # handle the response being a list of items
    if args:
        if type(args[0]) == list:
            response_string = json.dumps(args[0])
        # if the return is a string assume it's valid json
        elif type(args[0]) == str:
            response_string = json.dumps(args[0]) if callback else args[0]
    else:
        response_string = json.dumps(kwargs)

    if callback:
        response_string = "%s(%s);" % (callback, response_string)
        content_type = "application/javascript"
        # I know it's sucky but many clients will fail on jsonp requests
        # that return a 404
        if status_code == 404:
            status_code = 200

    headers = {"Content-Type": content_type, "Cache-Control": "no-cache", "Pragma": "no-cache"}

    return (
        response_string,
        status_code,
        headers
    )

def return_cors_response():
    headers = dict()
    headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS, DELETE, PATCH'
    headers['Access-Control-Allow-Headers'] = request.headers.get('Access-Control-Request-Headers', '*')
    return ( "", 204, headers )

def global_response_handler(response):
    response.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['X-HOSTNAME'] = app.config['X-HOSTNAME']
    response.headers['X-APP-VERSION'] = app.config['VERSION']
    return response

def global_request_handler():
    # this needs to be safe for use by the app
    g.user_token = secure_filename(request.headers.get(app.config['USER_HEADER_NAME'], 'DEFAULT'))

app.process_response = global_response_handler
app.preprocess_request = global_request_handler

################################################################################
# views

@app.route("/", methods=["GET"])
@app.route("/diag", methods=["GET"])
@app.route("/diagnostic", methods=["GET"])
def pyserver_core_diagnostic_view():
    """
        Used to return the status of the application, including the version
        of the running application.

        :statuscode 200: returned as long as all checks return healthy
        :statuscode 500: returned in the case of any diagnostic tests failing
    """
    diag_info = {}
    diag_info['machine_name'] = socket.gethostname()
    diag_info['process_start_time'] = process_start_time.isoformat()
    diag_info['process_uptime_secs'] = (datetime.datetime.now() - process_start_time).seconds
    diag_info['server_port'] = os.environ.get('PORT', None)

    response = make_response(json.dumps(diag_info, sort_keys=True))
    response.headers['X-Robots-Tag'] = 'noindex'
    return response

@app.route("/diagnostic/echo", methods=["GET"])
@make_my_response_json
def pyserver_core_diagnostic_echo_view():
    """
        Helper endpoint for developing diagnostic checks.  Simply echoes back
        any values provided in the inbound request.

        :param '*': any inbound request parameters will be echoed back
        :statuscode 200: always returns OK
    """
    return request.values.to_dict()

@app.route("/diagnostic/fail", methods=["GET"])
def pyserver_core_fail():
    """ This endpoint is designed to show how the application fails.  Can be used
        to assist in creating monitors to check the application health and respond
        to failures.

        :statuscode 500: always returns failure
    """
    raise Exception("Test exception so you know how the app behaves")

@app.route("/message/local_publish", methods=["POST"])
@make_my_response_json
def pyserver_core_publish_message():
    """ Allows for publishing of a local message.  """
    if request.json:
        msg = json.dumps(request.json)
    else:
        data = request.values.to_dict(flat=False)
        msg = convert_types_in_dictionary(remove_single_element_lists(data))
        msg = json.dumps(msg)
    messages.send(msg, messages.LOCAL_PUBLISH)
    return dict(message="ok")

# end views
################################################################################

@app.errorhandler(500)
def general_error_handler(error):
    et, ev, tb = sys.exc_info()
    assert ev == error
    eid = str(uuid4())
    logging.error("unhandled exception(%s):" % (eid), exc_info = (et, ev, tb))
    context = dict(eid=eid)
    if request.content_type and 'json' in request.content_type:
        return (render_template("500.json", **context),
            500,
            {"Content-Type": "application/json"})
    else:
        return render_template("500.html", **context), 500

from .core_handlers import token_handlers, echo

