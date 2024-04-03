import os
import sys
import json
from urllib.request import urlopen, urlretrieve
from string import Template
import requests
import re
from copy import deepcopy
from functools import reduce
from collections import defaultdict, namedtuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import subprocess
import warnings
from tempfile import mkdtemp
import time
import glob
from pathlib import Path

AKC_BUCKET="ci-audit-kind-conformance"
KGCL_BUCKET="ci-kubernetes-gce-conformance-latest"
KEGG_BUCKET="ci-kubernetes-e2e-gci-gce"
CONFORMANCE_RUNS="https://prow.k8s.io/job-history/kubernetes-jenkins/logs/"

AUDIT_KIND_CONFORMANCE_LOGS="https://storage.googleapis.com/kubernetes-jenkins/logs/ci-audit-kind-conformance"
GCS_LOGS="https://storage.googleapis.com/kubernetes-jenkins/logs/"

ARTIFACTS_PATH ='https://gcsweb.k8s.io/gcs/kubernetes-jenkins/logs/'
K8S_GITHUB_REPO = 'https://raw.githubusercontent.com/kubernetes/kubernetes/'

Meta = namedtuple('Meta',['job','version','commit','log_links','timestamp'])

IGNORED_PATHS=[
    'metrics',
    'readyz',
    'livez',
    'healthz',
    'example.com',
    'kope.io',
    'snapshot.storage.k8s.io',
    'metrics.k8s.io',
    'wardle.k8s.io'
]

def assign_verb_to_method (verb, uri):
    """Assigns audit event verb to apropriate method for generating opID later.
       Accounts for irregular behaviour with head and option verbs."""
    methods_and_verbs={
        'get': ['get','list','watch'],
        'proxy': ['proxy'],
        'options': [''],
        'post': ['create','post'],
        'put': ['update','put'],
        'patch': ['patch'],
        'connect': ['connect'],
        'delete': ['delete','delete_collection','deletecollection']
    }

    if verb == 'get' and uri.endswith('HEAD'):
        return 'head'

    for key, value in methods_and_verbs.items():
        if verb in value:
            return key
    return None

def get_json(url):
    """Given a json url path, return json as dict"""
    body = urlopen(url).read()
    data = json.loads(body)
    return data

def get_html(url):
    """return html content of given url"""
    html = urlopen(url).read()
    soup = BeautifulSoup(html, 'html.parser')
    return soup

def is_spyglass_script(tag):
    """does the soup tag contain a script that matches spyglass scripts?"""
    return tag.name == 'script' and not tag.has_attr('src') and ('allBuilds' in tag.contents[0])

def merge_into(d1, d2):
    for key in d2:
        if key not in d1 or not isinstance(d1[key], dict):
            d1[key] = deepcopy(d2[key])
        else:
            d1[key] = merge_into(d1[key], d2[key])
    return d1

def deep_merge(*dicts, update=False):
    if update:
        return reduce(merge_into, dicts[1:], dicts[0])
    else:
        return reduce(merge_into, dicts, {})

def download_url_to_path(url, local_path, dl_dict):
    """
    downloads contents to local path, creating path if needed,
    then updates given downloads dict.
    """
    local_dir = os.path.dirname(local_path)
    if not os.path.isdir(local_dir):
        os.makedirs(local_dir)
    if not os.path.isfile(local_path):
        process = subprocess.Popen(['wget', '-q', url, '-O', local_path])
        dl_dict[local_path] = process

def cluster_swagger():
    """Gets the swagger generated by a k8s api server, checking if incluster token is available"""
    url = "https://kubernetes/openapi/v2"
    in_cluster_tokenfile = "/opt/token.txt"
    tokenfile = "/token.txt"
    if os.path.isfile(in_cluster_tokenfile) and os.access(in_cluster_tokenfile, os.R_OK):
        token = Path(in_cluster_tokenfile).read_text()
    elif os.path.isfile(tokenfile) and os.access(tokenfile, os.R_OK):
        token = Path(tokenfile).read_text()
    else:
        token = None

    if token is None:
        swagger_url = "https://raw.githubusercontent.com/kubernetes/kubernetes/master/api/openapi-spec/swagger.json"
        return requests.get(swagger_url).json()
    else:
        auth = {"Authorization": "Bearer " + token}
        return requests.get(url, headers=auth, verify=False).json()

def load_openapi_spec(url):
    """
    Load given swagger url into a cache, so we can use it later to find operation id's
    """
    # Usually, a Python dictionary throws a KeyError if you try to get an item with a key that is not currently in the dictionary.
    # The defaultdict in contrast will simply return an empty dict.
    cache=defaultdict(dict)
    openapi_spec = {}
    openapi_spec['hit_cache'] = {}
    swagger = cluster_swagger() if url == 'cluster' else requests.get(url).json()
    # swagger contains other data, but paths is our primary target
    for path in swagger['paths']:
        # parts of the url of the 'endpoint'
        path_parts = path.strip("/").split("/")
        # how many parts?
        path_len = len(path_parts)
        # current_level = path_dict  = {}
        last_part = None
        last_level = None
        path_dict = {}
        current_level = path_dict
        # look at each part of the url/path
        for part in path_parts:
            # if the current level doesn't have a key (folder) for this part, create an empty one
            if part not in current_level:
                current_level[part] = {}
                 # current_level will be this this 'folder/dict', this might be empty
                # /api will be the top level v. often, and we only set it once
                current_level = current_level[part]
        for method, swagger_method in swagger['paths'][path].items():
            # If the method is parameters, we don't look at it
            # think this method is only called to explore with the dynamic client
            if method == 'parameters':
                next
            else:
                # for the nested current_level (end of the path/url) use the method as a lookup to the operationId
                current_level[method]=swagger_method.get('operationId', '')
                # cache = {}
                # cache = {3 : {'/api','v1','endpoints'}
                # cache = {3 : {'/api','v1','endpoints'} {2 : {'/api','v1'}
                # cache uses the length of the path to only search against other paths that are the same length
                cache = deep_merge(cache, {path_len:path_dict})
                openapi_spec['cache'] = cache
    return openapi_spec

def format_uri_parts_for_proxy(uri_parts):
    """
    take everything post proxy in a url and compose it into uri to compare against api spec
    """
    proxy = uri_parts.index('proxy')
    formatted_parts=uri_parts[0:proxy+1]
    proxy_tail = uri_parts[proxy+1:]
    if len(proxy_tail):
        formatted_parts.append('/'.join(proxy_tail))
    return formatted_parts

def is_namespace_status(uri_parts):
    if len(uri_parts) != 5:
        return False
    return uri_parts[2] == 'namespaces' and uri_parts[-1] == 'status'

def format_uri_parts_for_namespace_status(uri_parts):
    """
    Format uri for namespace endpoints for easier matchup with openapi spec
    """
    # in the open api spec, the namespace endpoints
    # are listed differently from other endpoints.
    # it abstracts the specific namespace to just {name}
    # so if you hit /api/v1/namespaces/something/cool/status
    # it shows in the spec as api.v1.namespaces.{name}.status
    uri_first_half = uri_parts[:3]
    uri_second_half =['{name}','status']
    return uri_first_half + uri_second_half

def is_namespace_finalize(uri_parts):
    if len(uri_parts) != 5:
        return False
    return uri_parts[2] == 'namespaces' and uri_parts[-1] == 'finalize'

def format_uri_parts_for_namespace_finalize(uri_parts):
    """
    Format uri for namespace finalize endpoints for easier matchup with openapi spec
    """
    # Using the same logic as status, but I am uncertain
    # all the various finalize endpoints, so this may not
    # pick them all up.  Revisit if so!
    uri_first_half = uri_parts[:3]
    uri_second_half =['{name}','finalize']
    return uri_first_half + uri_second_half

def format_uri_parts(path):
  """
  format uri parts for easier matchup with openapi spec
  """
  uri_parts = path.strip('/').split('/')
  if 'proxy' in uri_parts:
    uri_parts = format_uri_parts_for_proxy(uri_parts)
  elif is_namespace_status(uri_parts):
      uri_parts = format_uri_parts_for_namespace_status(uri_parts)
  elif is_namespace_finalize(uri_parts):
      uri_parts = format_uri_parts_for_namespace_finalize(uri_parts)
  return uri_parts

def is_ignored_endpoint(uri_parts):
    """is endpoint in our list of ignored paths?"""
    if any(part in uri_parts for part in IGNORED_PATHS):
        return True
    if uri_parts == ['openapi','v2']:
        return True
    return False

# given an open api spec and audit event, returns operation id and an error.
# If the opID can be found in the spec,
# then we return it with a nil error.
# Otherwise, we return a nilID and a given error message.
# we add both op id and error to our events,
# so that we can parse events by error in snoopdb
def find_operation_id(openapi_spec, event):
  """
  Take an openapi spec and an audit event and find the operation ID in the spec that matches the endpoint of the given eventk
  """
  method=assign_verb_to_method(event['verb'], event['requestURI'])
  if method is None:
      return None, "Could not assign a method from the event verb. Check the event.verb."
  url = urlparse(event['requestURI'])
  if(url.path in openapi_spec['hit_cache'] and
     method in openapi_spec['hit_cache'][url.path].keys()):
      return openapi_spec['hit_cache'][url.path][method], None
  uri_parts = format_uri_parts(url.path)
  part_count = len(uri_parts)
  if part_count in openapi_spec['cache']:
      cache = openapi_spec['cache'][part_count]
  else:
      return None, "part count too high, and not found in open api spec. Check the event's request URI"
  if is_ignored_endpoint(uri_parts):
      return None, 'This is a known dummy endpoint and can be ignored. See the requestURI for more info.'
  last_part = None
  last_level = None
  current_level = cache
  for idx in range(part_count):
    part = uri_parts[idx]
    last_level = current_level
    if part in current_level:
      current_level = current_level[part]
    elif idx == part_count-1:
      variable_levels=[x for x in current_level.keys() if '{' in x]
      if not variable_levels:
        return None, "We have not seen this type of event before, and it is not in spec. Check its request uri"
      variable_level=variable_levels[0]
      if variable_level in current_level:
          current_level = current_level[variable_level]
      else:
          return None, "Cannot find variable level in open api spec. Check the requestURI for more info"
    else:
      next_part = uri_parts[idx+1]
      variable_levels=[x for x in current_level.keys() if '{' in x]
      if not variable_levels:
        return None, "We have not seen this type of event before, and it is not in spec. Check its request uri"
      next_level=variable_levels[0]
      current_level = current_level[next_level]
  if method in current_level:
      op_id = current_level[method]
  else:
      return None, "Could not find operation for given method. Check the requestURI and the method."
  if url.path not in openapi_spec['hit_cache']:
    openapi_spec['hit_cache'][url.path]={method:op_id}
  else:
    openapi_spec['hit_cache'][url.path][method]=op_id
  return op_id, None

def bucket_latest_success(bucket):
    """
    determines latest successful run for ci-audit-kind-conformance and returns its ID as a string.
    """
    test_runs = CONFORMANCE_RUNS + bucket
    soup = get_html(test_runs)
    scripts = soup.find(is_spyglass_script)
    if scripts is None :
        raise ValueError("No spyglass script found in akc page")
    try:
        builds = json.loads(scripts.contents[0].split('allBuilds = ')[1][:-2])
    except Exception as e:
        raise ValueError("Could not load json from build data. is it valid json?", e)
    try:
        latest_success = [b for b in builds if b['Result'] == 'SUCCESS'][0]
    except Exception as e:
        raise ValueError("Cannot find success in builds")
    return latest_success['ID']

def akc_version(job):
    """return semver of kubernetes used for given akc job"""
    versionfile_path = "/artifacts/logs/kind-control-plane/kubernetes-version.txt"
    version_url =  AUDIT_KIND_CONFORMANCE_LOGS + "/" + job + versionfile_path
    version_file = urlopen(version_url).read().decode()
    # version_file will be something like v1.26.0-alpha.0.378+bcea98234f0fdc-dirty
    # We only want the k8s semver(in this example, the 1.26.0)
    # so, create a capture group of any number or '.' in between a starting 'v' and a '-'
    version = re.match("^v([0-9.]+)-",version_file).group(1)
    return version

def akc_commit(job):
    """return commit of kubernetes/kubernetes used for given akc job"""
    started_url = AUDIT_KIND_CONFORMANCE_LOGS + "/" + job + "/started.json"
    started = json.loads(urlopen(started_url).read().decode('utf-8'))
    return started["repo-commit"]

def akc_loglinks(job):
    """
    grab all the audit logs from our ci-audit-kind-conformance bucket,
    since their names and locations are non-standard
    """
    artifacts_url = ARTIFACTS_PATH + AKC_BUCKET + '/' +  job + '/' + 'artifacts/audit'
    soup = get_html(artifacts_url)
    return soup.find_all(href=re.compile(".log"))

def akc_timestamp(job):
    """return timestamp of when given akc job was run"""
    started_url = AUDIT_KIND_CONFORMANCE_LOGS + "/" + job + "/started.json"
    started = json.loads(urlopen(started_url).read().decode('utf-8'))
    return started["timestamp"]

def akc_meta(bucket, custom_job=None):
    """
    Compose a Meta object for job of given AKC bucket.
    Meta object contains the job, the k8s version, the k8s commit, the audit log links for the test run, and thed timestamp of the testrun
    """
    job = bucket_latest_success(bucket) if custom_job is None else custom_job
    return Meta(job,
                akc_version(job),
                akc_commit(job),
                akc_loglinks(job),
                akc_timestamp(job))

def kgcl_version(job):
    """
    return k8s semver for version of k8s run in given job's test run
    """
    finished_url = GCS_LOGS + KGCL_BUCKET + '/' + job + '/finished.json'
    finished = get_json(finished_url)
    job_version = finished["metadata"]["job-version"]

    match = re.match("^v([0-9.]+)-",job_version)
    if match is None:
        raise ValueError("Could not find version in given job_version.", job_version)
    else:
        version = match.group(1)
        return version

def kgcl_commit(job):
    """
    return k8s/k8s commit for k8s used in given job's test run
    """
    # we want the end of the string, after the '+'. A commit should only be numbers and letters
    finished_url = GCS_LOGS + KGCL_BUCKET + '/' + job + '/finished.json'
    finished = get_json(finished_url)
    job_version = finished["metadata"]["job-version"]

    match = re.match(".+\+([0-9a-zA-Z]+)$",job_version)
    if match is None:
        raise ValueError("Could not find commit in given job_version", job_version)
    else:
        commit = match.group(1)
        return commit

def kgcl_loglinks(job):
    """Return all audit log links for KGCL bucket"""
    artifacts_url = ARTIFACTS_PATH + KGCL_BUCKET + '/' +  job + '/' + 'artifacts'
    soup = get_html(artifacts_url)
    master_link = soup.find(href=re.compile("master"))
    master_soup = get_html("https://gcsweb.k8s.io" + master_link['href'])
    return master_soup.find_all(href=re.compile("audit.log"))

def kgcl_timestamp(job):
    """
    Return unix timestamp of when given job was run
    """
    finished_url = GCS_LOGS + KGCL_BUCKET + '/' + job + '/finished.json'
    finished = get_json(finished_url)
    return finished["timestamp"]

def kgcl_meta(bucket, custom_job=None):
    """
    Compose a Meta object for job of given KGCL bucket.
    Meta object contains the job, the k8s version, the k8s commit, the audit log links for the test run, and thed timestamp of the testrun
    """
    job = bucket_latest_success(bucket) if custom_job is None else custom_job
    return Meta(job,
                kgcl_version(job),
                kgcl_commit(job),
                kgcl_loglinks(job),
                kgcl_timestamp(job))

def kegg_version(job):
    """
    return k8s semver for version of k8s run in given job's test run
    """
    finished_url = GCS_LOGS + KEGG_BUCKET + '/' + job + '/finished.json'
    finished = get_json(finished_url)
    job_version = finished["metadata"]["job-version"]

    match = re.match("^v([0-9.]+)-",job_version)
    if match is None:
        raise ValueError("Could not find version in given job_version.", job_version)
    else:
        version = match.group(1)
        return version

def kegg_commit(job):
    """
    return k8s/k8s commit for k8s used in given job's test run
    """
    # we want the end of the string, after the '+'. A commit should only be numbers and letters
    finished_url = GCS_LOGS + KEGG_BUCKET + '/' + job + '/finished.json'
    finished = get_json(finished_url)
    job_version = finished["metadata"]["job-version"]
    match = re.match(".+\+([0-9a-zA-Z]+)$",job_version)
    if match is None:
        raise ValueError("Could not find commit in given job_version.", job_version)
    else:
        commit = match.group(1)
        return commit

def kegg_loglinks(job):
    """Return all audit log links for KEGG bucket"""
    artifacts_url = ARTIFACTS_PATH + KEGG_BUCKET + '/' +  job + '/' + 'artifacts'
    soup = get_html(artifacts_url)
    master_link = soup.find(href=re.compile("master"))
    master_soup = get_html("https://gcsweb.k8s.io" + master_link['href'])
    return master_soup.find_all(href=re.compile("audit.log"))

def kegg_timestamp(job):
    """
    Return unix timestamp of when given job was run
    """
    finished_url = GCS_LOGS + KEGG_BUCKET + '/' + job + '/finished.json'
    finished = get_json(finished_url)
    return finished["timestamp"]

def kegg_meta(bucket, custom_job=None):
    """
    Compose a Meta object for job of given KEGG bucket.
    Meta object contains the job, the k8s version, the k8s commit, the audit log links for the test run, and thed timestamp of the testrun
    """
    job = bucket_latest_success(bucket) if custom_job is None else custom_job
    return Meta(job,
                kegg_version(job),
                kegg_commit(job),
                kegg_loglinks(job),
                kegg_timestamp(job))

def get_meta(bucket,job=None):
    """Returns meta object for given bucket.
    Meta includes job, k8s version, k8s commit, all auditlog links, and timestamp of the test run"""
    if(bucket == AKC_BUCKET):
        return akc_meta(bucket,job)
    elif(bucket == KGCL_BUCKET):
        return kgcl_meta(bucket,job)
    elif(bucket == KEGG_BUCKET):
        return kegg_meta(bucket, job)

def download_and_process_auditlogs(bucket,job):
    """
    Grabs all audits logs available for a given bucket/job, combines them into a
    single audit log, then returns the path for where the raw combined audit logs are stored.
    The processed logs are in json, and include the operationId when found.
    """
    downloads = {}
    # bucket_url = BUCKETS_PATH + bucket + '/' + job + '/'
    download_path = mkdtemp( dir='/tmp', prefix='apisnoop-' + bucket + '-' + job ) + '/'
    combined_log_file = download_path + 'combined-audit.log'
    meta = get_meta(bucket,job)

    for link in meta.log_links:
        log_url = link['href']
        log_file = download_path + os.path.basename(log_url)
        download_url_to_path(log_url, log_file, downloads)

    # Our Downloader uses subprocess of curl for speed
    for download in downloads.keys():
        # Sleep for 5 seconds and check for next download
        while downloads[download].poll() is None:
            time.sleep(5)

    # Loop through the files, (z)cat them into a combined audit.log
    with open(combined_log_file, 'ab') as log:
        glob_pattern = 'audit*log' if bucket == AKC_BUCKET else '*kube-apiserver-audit*'
        for logfile in sorted(glob.glob(download_path + glob_pattern), reverse=True):
            if logfile.endswith('z'):
                subprocess.run(['zcat', logfile], stdout=log, check=True)
            else:
                subprocess.run(['cat', logfile], stdout=log, check=True)

    # Process the resulting combined raw audit.log by adding operationId
    swagger_url = K8S_GITHUB_REPO + meta.commit + '/api/openapi-spec/swagger.json'
    openapi_spec = load_openapi_spec(swagger_url)
    infilepath=combined_log_file
    outfilepath=combined_log_file+'+opid'
    with open(infilepath) as infile:
        with open(outfilepath,'w') as output:
            for line in infile.readlines():
                event = json.loads(line)
                opId, err = find_operation_id(openapi_spec,event)
                event['operationId'] = opId
                event['snoopError'] = err
                output.write(json.dumps(event)+'\n')
    return outfilepath
