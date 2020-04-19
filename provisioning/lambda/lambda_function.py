# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


import base64
import boto3
import json
import logging
import os
import re
import requests
import string
import sys
import time
from OpenSSL import crypto
from OpenSSL.crypto import X509
from time import gmtime, strftime
from geopy.distance import great_circle

# globals
# ipstack_api_url = 'http://api.ipstack.com/'
# ipstack_api_key = os.environ['IPSTACK_API_KEY']

iot_policy_name = 'GlobalDevicePolicy'
dynamodb_table_name = 'iot-global-provisioning-v1'
pub_key_file = 'global-provisioning.pub.key.pem'

# Configure logging
logger = logging.getLogger()

for h in logger.handlers:
    logger.removeHandler(h)
h = logging.StreamHandler(sys.stdout)

FORMAT = "[%(asctime)s - %(levelname)s - %(filename)s:%(lineno)s - %(funcName)s - %(message)s"
h.setFormatter(logging.Formatter(FORMAT))

logger.addHandler(h)
logger.setLevel(logging.INFO)

class RequestIdAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return '%s]: %s' % (self.extra['request_id'], msg), kwargs

regions = [
    {"name": "ap-northeast-1", "lat": "35.9", "lon": "140.0"},
    {"name": "eu-west-1", "lat": "53.5", "lon": "-6.1"},
    {"name": "ap-southeast-2", "lat": "-33.7", "lon": "151.4"},
    {"name": "us-east-2", "lat": "40.4", "lon": "-82.5"},
    {"name": "eu-central-1", "lat": "50.3", "lon": "8.9"},
    {"name": "us-east-1", "lat": "32.4", "lon": "-98.0"},
    {"name": "ap-northeast-2", "lat": "37.8", "lon": "127.2"},
    {"name": "ap-southeast-1", "lat": "1.5", "lon": "104.1"},
    {"name": "ap-south-1", "lat": "19.1", "lon": "73.0"},
    {"name": "us-west-2", "lat": "44.2", "lon": "-120.5"},
    {"name": "eu-west-2", "lat": "51.7", "lon": "0.1"}
]

default_region = "us-west-2"

# def get_ip_location(ip):
#     request_url = ipstack_api_url + '/' + ip + '?access_key=' + ipstack_api_key
#     logger.debug("request_url: {}".format(request_url))
#     r = requests.get(request_url)
#     j = json.loads(r.text)
#     logger.debug("j: {}".format(j))
#     return j


def find_best_region(lat, lon):
    min_distance = 40000
    closest_region = None

    for r in regions:
        logger.debug("r: {}".format(r))
        elat = float(r["lat"])
        elon = float(r["lon"])
        logger.debug("elat: {}, elon: {}".format(elat, elon))
        distance = great_circle((lat, lon), (elat, elon)).km
        logger.debug("distance: {}".format(distance))
        if distance <= min_distance:
            min_distance = distance
            closest_region = r["name"]
        logger.debug("min_distance: {}".format(min_distance))

    logger.info("closest_region: {}, distance: {}".format(closest_region, min_distance))

    return {"region": closest_region, "distance": min_distance}


def get_account_id():
    client = boto3.client('sts')
    response = client.get_caller_identity()
    logger.info("response: {}".format(response))
    return response['Account']


def create_iot_policy_if_missing(c_iot, region):
    try:
        response = c_iot.get_policy(policyName = iot_policy_name)
        logger.info("policy exists already: response: {}".format(response))
    except Exception as e:
        if re.match('.*ResourceNotFoundException.*', str(e)):
            logger.info("creating iot policy {}".format(iot_policy_name))
            account_id = get_account_id()
            arn_connect = 'arn:aws:iot:' + region + ':' + account_id + ':client/${iot:ClientId}'
            arn_publish = 'arn:aws:iot:' + region + ':' + account_id + ':topic/data/${iot:ClientId}/*'
            logger.info("arn_connect: {}".format(arn_connect))
            logger.info("arn_publish: {}".format(arn_publish))

            policy_document = '''{
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": ["iot:Connect"],
                    "Resource": [ "''' + arn_connect + '''" ]
                },
                {
                    "Effect": "Allow",
                    "Action": ["iot:Publish"],
                    "Resource": [ "''' + arn_publish + '''" ]
                }]
            }'''

            response = c_iot.create_policy(
                policyName = iot_policy_name,
                policyDocument = policy_document
            )
            logger.info("response: {}".format(response))
        else:
            logger.error("unknown error: {}".format(e))

def get_policy_name(thing_name, identity_id):
    return identity_id.replace(":", "@") + thing_name.replace(":", "@").replace("_", "-")

def create_iot_policy_for_user_if_missing(c_iot, thing_name, identity_id):
    # create policy to be attached to specified identity_id
    policy_name = get_policy_name(thing_name, identity_id)
    try:
        response = c_iot.get_policy(policyName = policy_name)
        logger.info("policy exists already: response: {}".format(response))
    except Exception as e:
        if re.match('.*ResourceNotFoundException.*', str(e)):
            logger.info("creating iot policy {}".format(policy_name))

            policy_document = '''{
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "iot:Connect", 
                        "Resource": "arn:aws:iot:us-east-2:443188300111:*" 
                    }, 
                    {
                        "Effect": "Allow",
                        "Action": [
                            "iot:Receive",
                            "iot:Publish",
                            "iot:Subscribe"
                        ],
                        "Resource": [ 
                            "arn:aws:iot:us-east-2:443188300111:topic/$aws/things/''' + thing_name  + '''/*",
                            "arn:aws:iot:us-east-2:443188300111:topicfilter/$aws/things/''' + thing_name + '''/*" 
                        ] 
                    },
                    { 
                        "Effect": "Allow", 
                        "Action": [ 
                            "iot:GetThingShadow", 
                            "iot:UpdateThingShadow" 
                        ], 
                        "Resource": "arn:aws:iot:us-east-2:443188300111:thing/''' + thing_name + '''" 
                    } 
                ]
            }'''

            response = c_iot.create_policy(
                policyName = policy_name,
                policyDocument = policy_document
            )
            logger.info("response: {}".format(response))
        else:
            logger.error("unknown error: {}".format(e))

    return policy_name


def provision_device(thing_name, sn, version, region, CSR, identity_id, provisioned_info):
    thing_name = thing_name.replace(" ", "-") + "-" + sn.replace(":", "-").replace("_", "-")
    has_previous_user = 'user_id' in provisioned_info

    answer = {}
    logger.info("thing_name: {}, region {}".format(thing_name, region))
    c_iot = boto3.client('iot', region_name = region)

    # endpoint
    response = c_iot.describe_endpoint(endpointType='iot:Data-ATS')
    logger.info("response: {}".format(response))
    answer['endpointAddress'] = response['endpointAddress']

    # create policy if missing
    create_iot_policy_if_missing(c_iot, region)

    # create thing
    if (not has_previous_user):
        response = c_iot.create_thing(thingName = thing_name)
        logger.info("create_thing response: {}".format(response))

    if (has_previous_user):
        previous_policy_name = get_policy_name(thing_name, provisioned_info['user_id'])
        c_iot.detach_policy(
            policyName = previous_policy_name,
            target = identity_id
        )
        # delete policy is too dangerous
        # c_iot.delete_policy(
        #     policyName = previous_policy_name, 
        # )

    # create policy 
    policy_name = create_iot_policy_for_user_if_missing(c_iot, thing_name, identity_id) 

    # attach the polity to identity_id
    response = c_iot.attach_policy(
        policyName = policy_name,
        target = identity_id
    )
    logger.info("attach_policy to user response: {}".format(response)) 

    if CSR:
        logger.info("CSR received: create_certificate_from_csr")
        # create cert from csr
        response = c_iot.create_certificate_from_csr(
            certificateSigningRequest = CSR,
            setAsActive = True
        )
        logger.debug("create certificate from csr response: {}".format(response))
        certificate_arn = response['certificateArn']
        certificate_id = response['certificateId']
        logger.info("certificate_arn: {}, certificate_id: {}".format(certificate_arn, certificate_id))
        answer['certificatePem'] = response['certificatePem']
    else:
        logger.info("no CSR received: create_keys_and_certificate")
        # create key/cert
        response = c_iot.create_keys_and_certificate(setAsActive = True)
        logger.debug("create keys and certificate response: {}".format(response))
        certificate_arn = response['certificateArn']
        certificate_id = response['certificateId']
        logger.info("certificate_arn: {}, certificate_id: {}".format(certificate_arn, certificate_id))
        answer['certificatePem'] = response['certificatePem']
        answer['PrivateKey'] = response['keyPair']['PrivateKey']

    if (has_previous_user):
        # detatch from policy and thing_principal 
        try:
            response = c_iot.detach_policy(
                policyName = iot_policy_name,
                target = provisioned_info['certificate_arn']
            )
            logger.info("detach policy response: {}".format(response))
        except Exception as e:
            logger.error("detach policy failed: {}".format(e))

        try:
            response = c_iot.detach_thing_principal(
                thingName = thing_name,
                principal = provisioned_info['certificate_arn']
            )
            logger.info("attach thing principal response: {}".format(response)) 
        except Exception as e:
            logger.error("detach thing principal failed: {}".format(e))

        # # set to inactivel
        # response = c_iot.update_certificate(
        #     principal = provisioned_info['certificate_id'],
        #     newStatus='INACTIVE'
        # )
        # logger.info("set certificate to inactive response: {}".format(response)) 

        # # delete the certificate
        # c_iot.delete_certificate(
        #     principal = provisioned_info['certificate_id'], 
        #     forceDelete=True
        # )

    # attach policy to certificate
    response = c_iot.attach_policy(
        policyName = iot_policy_name,
        target = certificate_arn
    )
    logger.info("attach policy response: {}".format(response))

    response = c_iot.attach_thing_principal(
        thingName = thing_name,
        principal = certificate_arn
    )
    logger.info("attach thing principal response: {}".format(response))

    # bring these value back to update DB
    answer['certificate_id'] = certificate_id
    answer['certificate_arn'] = certificate_arn
    answer['thing_name'] = thing_name

    return answer


def device_pub_key_pem_for_provisioning(sn):
    c_dynamo = boto3.client('dynamodb')
    key = {"sn": {"S": sn}}
    logger.info("key {}".format(key))

    response = c_dynamo.get_item(TableName = dynamodb_table_name, Key = key)
    logger.info("response: {}".format(response))

    if 'Item' in response:
        if 'pub_key_pem' in response['Item']:
            pub_key_pem = response['Item']['pub_key_pem']['S']
            logger.info("pub_key_pem: {}".format(pub_key_pem))
            return pub_key_pem
        else:
            logger.warn("no pub_key_pem in result")
            return ""
    else:
        logger.error("thing {} not found in DynamoDB".format(sn))

    return "" 


def device_provisioned_to(sn):
    c_dynamo = boto3.client('dynamodb')
    key = {"sn": {"S": sn}}
    logger.info("key {}".format(key))

    response = c_dynamo.get_item(TableName = dynamodb_table_name, Key = key)
    logger.info("response: {}".format(response))

    result = {}
    if 'Item' in response:
        result['sn'] = sn
        item = response['Item'] 
        if 'prov_status' in item:
            status = item['prov_status']['S']
            logger.info("status: {}".format(status))
            if status == "provisioned":
                result['user_id'] = item['user_id']['S']
                result['certificate_id'] = item['certificate_id']['S']
                result['certificate_arn'] = item['certificate_arn']['S']

                return result
        else:
            logger.warn("no status in result")
    else:
        logger.error("thing {} not found in DynamoDB".format(sn))

    return result


def update_device_provisioning_status(sn, region, thing_name, version, identity_id, user_name, other):
    c_dynamo = boto3.client('dynamodb')
    datetime = time.strftime("%Y-%m-%dT%H:%M:%S%z", gmtime())
    # to follow the W3C Date and Time Formats -- https://www.w3.org/TR/NOTE-datetime
    datetime = datetime[:-2] + ':' + datetime[-2:] 

    key = {"sn": {"S": sn}}
    logger.info("key {}".format(key))
    update_expression = "SET prov_status = :s, prov_datetime = :d, aws_region = :r, alias_name = :an, version = :v, " \
        "user_id = :i, certificate_id = :ci, certificate_arn = :ca, thing_name = :tn, user_name= :un"
    expression_attribute_values = {":s": {"S": "provisioned"}, ":d": {"S": datetime}, ":r": {"S": region},
    ":an": {"S": thing_name}, ":v": {"S": version}, ":i": {"S": identity_id}, ":ci": {"S": other['certificate_id']}, 
    ":ca": {"S": other['certificate_arn']}, ":tn": {"S": other["thing_name"]}, ":un": {"S": user_name}}

    logger.info("expression_attribute_values: {}".format(expression_attribute_values))

    response = c_dynamo.update_item(
        TableName = dynamodb_table_name,
        Key = key,
        UpdateExpression = update_expression,
        ExpressionAttributeValues = expression_attribute_values
    )
    logger.info("response: {}".format(response))


def sig_verified(message, sig):
    # f = open(pub_key_file, 'r')
    # pub_key_pem = f.read()
    # f.close()

    # get pub_key_pem from DB
    pub_key_pem = device_pub_key_pem_for_provisioning(message) 

    pub_key = crypto.load_publickey(crypto.FILETYPE_PEM, pub_key_pem)
    sig = base64.b64decode(sig)
    pub_key_x509 = X509()
    pub_key_x509.set_pubkey(pub_key)

    try:
        crypto.verify(pub_key_x509, sig, message, 'sha256')
        logger.info("signature verified for message {}".format(message))
        return True
    except Exception as e:
        logger.error("verifying signature failed for message {}: {}".format(message, e))


def lambda_handler(event, context):
    global logger
    logger = RequestIdAdapter(logger, {'request_id': context.aws_request_id})

    logger.info("event: {}".format(event))

    thing_name = None
    sn = None
    # optional
    version = '1.0'
    sn_sig = None
    CSR = None
    identity_id = None
    user_name = None
    answer = {}

    if 'body-json' in event:
        if 'thing-name' in event['body-json']:
            thing_name = event['body-json']['thing-name']

        if 'sn-sig' in event['body-json']:
            sn_sig = event['body-json']['sn-sig']
        
        if 'sn' in event['body-json']:
            sn = event['body-json']['sn']

        if 'version' in event['body-json']:
            version = event['body-json']['version']

        if 'identity-id' in event['body-json']:
            identity_id = event['body-json']['identity-id']

        if 'user-name' in event['body-json']:
            user_name = event['body-json']['user-name']

        if 'CSR' in event['body-json']:
            CSR = event['body-json']['CSR']
    else:
        logger.error("invalid request: key body-json not found in event")
        return {"status": "error", "message": "invalid request"}


    logger.info("thing_name: {}".format(thing_name))
    logger.info("sn_sig: {}".format(sn_sig))
    logger.info("sn: {}".format(sn))
    logger.info("version: {}".format(version))
    logger.info('identity-id: {}'.format(identity_id))
    logger.info("user_name: {}".format(user_name))
    logger.info("CSR: {}".format(CSR))

    if thing_name == None:
        logger.error("no thing-name in request")
        return {"status": "error", "message": "no thing name"}

    if sn_sig == None:
        logger.error("no sn-sig in request")
        return {"status": "error", "message": "no sig"}

    if not sig_verified(sn, sn_sig):
        logger.error("signature could not be verified")
        return {"status": "error", "message": "wrong sig"}

    if sn == None:
        logger.error("no sn provided")
        return {"status": "error", "message": "no sn"}

    if identity_id == None:
        logger.error("no identity id provided to bind device to")
        return {"status": "error", "message": "no identity id"}

    if user_name == None:
        logger.error("no user name  provided to bind device to")
        return {"status": "error", "message": "no user name"}

    # if 'params' in event and 'header' in event['params'] and 'X-Forwarded-For' in event['params']['header']:
    #     device_addrs = str(event['params']['header']['X-Forwarded-For']).translate(None, string.whitespace).split(',')
    #     logger.info(device_addrs)
    # else:
    #     logger.warn("can not find X-Forwarded-For")
    #     return {"status": "error", "message": "no location"}

    # location = get_ip_location(device_addrs[0])
    # if location['latitude'] == None or location['longitude'] == None:
        # logger.warn("no latitude or longitude for IP {}, using default region {}".format(device_addrs[0], default_region))
        # logger.info("use fixed aws region for current implementation")
        # answer = provision_device(thing_name, default_region, CSR, identity_id)
        # answer['region'] = default_region
        # answer['message'] = "no latitude or longitude for IP {}, using default region {}".format(device_addrs[0], default_region)
        # answer['status'] = 'success'
    # else:
    #     lat = float(location['latitude'])
    #     lon = float(location['longitude'])
    #     logger.info("lat: {}, lon: {}".format(lat, lon))
    #     best_region = find_best_region(lat, lon)
    #     answer = provision_device(thing_name, best_region['region'], CSR, identity_id)
    #     answer['region'] = best_region['region']
    #     answer['distance'] = best_region['distance']
    #     answer['status'] = 'success'


    provisioned_info = device_provisioned_to(sn)
    if (not 'sn' in provisioned_info):
        answer['error'] = 'Cound not find device in DB. Contact the manufacturer, please'
        return answer
    
    logger.info("use fixed aws region for current implementation")
    answer = provision_device(thing_name, sn, version, default_region, CSR, identity_id, provisioned_info)
    answer['region'] = default_region
    # answer['message'] = "no latitude or longitude for IP {}, using default region {}".format(device_addrs[0], default_region)
    answer['status'] = 'success'
    # update_device_provisioning_status(sn, best_region['region'])
    update_device_provisioning_status(sn, default_region, thing_name, version, identity_id, user_name, answer)

    answer.pop('certificate_id', None)
    answer.pop('certificate_arn', None)

    return answer
