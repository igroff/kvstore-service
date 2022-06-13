import os
import uuid
import json
import time
import shutil
import logging
from os import path
from functools import wraps
import boto
from boto.exception import S3ResponseError, JSONResponseError
from boto.dynamodb2.table import Table
from boto.dynamodb2.fields import HashKey
from boto.dynamodb2.exceptions import ItemNotFound, ValidationException
from flask import request, make_response
from pyserver.core import app, make_my_response_json, convert_types_in_dictionary, remove_single_element_lists

DDB_TABLE = os.environ.get('TOKEN_DYNAMO_TABLE')
DDB_EXPIRED_TABLE = os.environ.get('TOKEN_EXPIRED_DYNAMO_TABLE')

if not DDB_TABLE:
    raise Exception('You must provide a ddb table to use with the token service')
else:
    logging.info("using ddb table %s for token service" % (DDB_TABLE))
    ddb_kvstore = Table(DDB_TABLE)
    try:
        ddb_kvstore.describe()
    except JSONResponseError, e:
        if "resource not found" in e.message:
            logging.info("creating ddb table %s for use with token service" %(DDB_TABLE))
            ddb_kvstore = Table.create(DDB_TABLE,
                [HashKey('path')],
                dict(read=1, write=1)
            )

if not DDB_EXPIRED_TABLE:
    raise Exception('You must provide a ddb table to use with the token service')
else:
    logging.info("using ddb table %s for token service" % (DDB_EXPIRED_TABLE))
    ddb_kvstore = Table(DDB_EXPIRED_TABLE)
    try:
        ddb_kvstore.describe()
    except JSONResponseError, e:
        if "resource not found" in e.message:
            logging.info("creating ddb table %s for use with token service" %(DDB_EXPIRED_TABLE))
            ddb_kvstore = Table.create(DDB_EXPIRED_TABLE,
                [HashKey('path')],
                dict(read=1, write=1)
            )

ddb_table = Table(DDB_TABLE)
ddb_expired_table = Table(DDB_EXPIRED_TABLE)
# Remove this when we're done with migration.
app.config['DATA_FOLDER'] = os.environ.get('DATA_FOLDER', '/home/glgapp/core_share/token-service/active')

################################################################################
# core

class InvalidTokenException(Exception):
    def __init__(self, *args, **kwargs):
        Exception(self, *args, **kwargs)

def create_token():
    return str(uuid.uuid4())

def get_old_path_for_token(token):
    token_path = path.join(app.config['DATA_FOLDER'], token[:2], token[2:4])
    return path.join(token_path, token)

def get_path_for_token(token):
    token_path = path.join(token[:2], token[2:4])
    return path.join(token_path, token)

def write_token_data(token, data, expiration):
    # expiration comes in as ms, so convert to seconds and
    # add to the current time
    expiration_time = int(expiration + time.time())
    path = get_path_for_token(token)
    ddb_table.put_item(data={'path':path, 'body':data, 'expiration':expiration_time}, overwrite=True)

def read_token_data(token):
    path = get_path_for_token(token)
    try:
        item = ddb_table.get_item(path=path)
        expiration = int(item['expiration'])
        if expiration < time.time():
            expire_token_data(item)
            raise InvalidTokenException(token)
        return item['body']
    except ItemNotFound:
        logging.error("unable to find token %s", token)
        raise InvalidTokenException(token) 

def read_expired_token_data(token):
    path = get_path_for_token(token)
    try:
        item = ddb_expired_table.get_item(path=path)
        return item['body']
    except ItemNotFound:
        logging.error("unable to find expired token %s", token)
        raise InvalidTokenException(token) 

def expire_token_data(ddb_item):
    ddb_expired_table.put_item(data={'path':ddb_item['path'], 'body':ddb_item['body'], 'expiration':ddb_item['expiration']}, overwrite=True)
    ddb_item.delete() # this is just for logical correctness, but as write overwrites... not necessary

def expire_token(token):
    path = get_path_for_token(token)
    try:
        item = ddb_table.get_item(path=path)
        expire_token_data(item)
    except ItemNotFound:
        pass

# end core
################################################################################

@app.route('/create', methods=['OPTIONS'])
def allow_cors(keys='', key='', data=''):
    response = make_response(data)
    response.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = request.headers.get('Access-Control-Request-Headers', '*')
    return response


@app.route("/create", methods=["POST", "GET"])
@make_my_response_json
def create_token_view():
    """ Creates a token referencing all parameters provided in the request as a JSON object.

        i.e. Any parameters passed in the query string or form areas of the request will be
        persisted and returned as key=value members of a JSON object

        :arg expiration_seconds: Expiration time in milliseconds such that expiration time is calculated
            by adding the value of this parameter to the time that the token was created

        :statuscode 200: Successful creation of token
        :statuscode 500: Missing required parameter (expiration)

    """
    expiration = request.values.get('expiration_seconds', None) or request.json.get('expiration_seconds', None)
    if not expiration:
        return "", 500
    expiration = int(expiration)
    token = create_token()
    inbound_values = request.json or convert_types_in_dictionary(remove_single_element_lists(request.values.to_dict(flat=False)))
    inbound_values['expiration'] = int(expiration + time.time())
    # strip the jsonp callback if one was provided as it can screw up the results?
    if 'callback' in inbound_values: del inbound_values['callback']
    write_token_data(token, json.dumps(inbound_values), expiration)
    return dict(message="ok", token=token)

@app.route("/validate/<token>", methods=["GET"])
@make_my_response_json
def validate_token_view(token):
    """ for a given token, return the data that was provided during the creation process
        The response will be either a JSON object containing all the values
        provided during token creation or an object with a single ``message`` property
        containing the string ``invalid token`` with a HTTP status of 410

        :statuscode 200: the token provided is valid, the respons will contain the JSON
            representation of the original data provided upon token creation
        :statuscode 410: the token provided refers to an expired or otherwise invalid token
    """
    try:
        return json.loads(read_token_data(token))
    except InvalidTokenException:
        return dict(message="invalid token", status_code=410)

@app.route("/expire/<token>", methods=["GET"])
@make_my_response_json
def expire_token_view(token):
    """ explicitly expire a token regardless of its expiration date """
    expire_token(token)
    return dict(message="ok")

@app.route("/update_expiration/<token>", methods=["POST", "GET"])
@make_my_response_json
def update_token_view(token):
    """ update token expiration date """
    path = get_path_for_token(token)
    try:
        expiration = request.values.get('expiration_seconds', None) or request.json.get('expiration_seconds', None)
        if not expiration:
            return "", 401
        tokenData = ddb_table.get_item(path=path)
        expiration = int(expiration)
        expiration_time = int(expiration + time.time())
        body = json.loads(tokenData['body'])
        body['original_expiration'] = str(tokenData['expiration'])
        body['expiration_seconds'] = str(expiration)
        body['expiration'] = expiration_time
        tokenData['body'] = json.dumps(body)
        tokenData['expiration'] = expiration_time
        tokenData.save()
        return dict(message="ok", token=token, expiration=expiration_time)
    except InvalidTokenException:
        return dict(message="invalid token")

@app.route("/get_expired/<token>", methods=["GET"])
@make_my_response_json
def get_expired_token_data_view(token):
    """ retrieve the data related to an expired token, this will NOT return data for an
        unexpired token
    """
    try:
        return json.loads(read_expired_token_data(token))
    except InvalidTokenException:
        return dict(message="invalid token")
