# Source of truth: parsed from actual XML files in rdkcentral manifest repos:
#   rdkcentral/rdke-oss-manifest       → rdk-oss.xml, rdk-docker.xml
#   rdkcentral/rdk-oss-common-manifest → rdk-oss-common.xml

GITHUB_ORG = "rdkcentral"

OSS_LAYER_JOBS = {
    "build-scripts": {
        "layer": "build-scripts",
        "manifest_project_name": "build-scripts",
    },
    "rdke-common-config": {
        "layer": "rdke-common-config",
        "manifest_project_name": "rdke-common-config",
    },
    "meta-native-prebuilt-generator": {
        "layer": "meta-native-prebuilt-generator",
        "manifest_project_name": "meta-native-prebuilt-generator",
    },
    "meta-stack-layering-support": {
        "layer": "meta-stack-layering-support",
        "manifest_project_name": "meta-stack-layering-support",
    },
    "rdk-oss-common-manifest": {
        "layer": "rdk-oss-common-manifest",
        "manifest_project_name": "rdk-oss-common-manifest",
    },
}

# ── Layers inside rdk-oss-common.xml (submanifest)
# monitor_branch: branch to watch for commits. Defaults to OSS_MONITOR_BRANCH ("develop")
#   if not specified. Yocto upstream forks use release branch names, not "develop".
OSS_COMMON_LAYER_JOBS = {
    "meta-rdk-auxiliary": {},
    "meta-oss-reference-development": {},
    "meta-rdk-oss-reference": {},
    "meta-openembedded":   {"monitor_branch": "rdk/kirkstone"},
    "poky":                {"monitor_branch": "rdk/kirkstone"},
    "meta-python2":        {"monitor_branch": "kirkstone"},
    "meta-clang":          {"monitor_branch": "rdk/kirkstone"},
    "meta-lts-mixins":     {"monitor_branch": "rdk/kirkstone"},
    "meta-rdk-bluetooth": {},
    "meta-binder": {},
}

# Manifest repository that holds rdk-oss.xml and rdk-docker.xml
OSS_MANIFEST_REPO = "rdke-oss-manifest"

# Common manifest repo that holds rdk-oss-common.xml
OSS_COMMON_MANIFEST_REPO = "rdk-oss-common-manifest"
OSS_COMMON_MANIFEST_XML  = "rdk-oss-common.xml"

# Default branch to monitor for changes in each layer repo
OSS_MONITOR_BRANCH = "develop"

# XML files to scan and update inside rdke-oss-manifest
OSS_MANIFEST_XML_FILES = ["rdk-oss.xml", "rdk-docker.xml"]
