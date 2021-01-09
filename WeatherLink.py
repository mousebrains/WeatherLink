#! /usr/bin/env python3
#
# Weatherlink API interface class
#
# The config file is a dictionary like:
#
# apiSecret: "12qd"
# apiKey: "xyza"
#
# Jan-2021, Pat Welch, pat@mousebrains.com

import argparse
import os.path
import time
import hashlib
import hmac
import requests
import yaml
import json

class V2:
    def __init__(self, args:argparse.ArgumentParser) -> None:
        self.__base = args.baseURL
        fn = os.path.abspath(os.path.expanduser(args.config))
        with open(fn, "r") as fp:
            self.__config = yaml.safe_load(fp)

    @staticmethod
    def addArgs(parser:argparse.ArgumentParser) -> argparse.ArgumentParser:
        grp = parser.add_argument_group(description="WeatherLink V2 API options")
        grp.add_argument("--config", type=str, default="~/.weatherlink/config.v2.yml",
                help="YAML configuration file with api secret and key")
        grp.add_argument("--baseURL", type=str, default="https://api.weatherlink.com/v2",
                help="Base URL for generating API URLs")
        return parser

    def __mkURL(self, action:str, pathName:str=None, pathVal:str=None, query:dict=None) -> tuple:
        url = self.__base + "/" + action

        args = {}
        if query is not None: args |= query
        args["api-key"] = self.__config["key"]
        args["t"] = int(time.time())

        params = {}
        if pathVal is not None: 
            params[pathName] = pathVal
            url += "/" + str(pathVal)
        params |= args

        a = hmac.new(self.__config["secret"].encode("utf-8"), digestmod=hashlib.sha256)
        for key in sorted(params):
            a.update(key.encode("utf-8"))
            a.update(str(params[key]).encode("utf-8"))

        args["api-signature"] = a.hexdigest()

        return (url, args)

    def __get(self, url:str, args:dict=None) -> dict:
        r = requests.get(url, args)
        info = r.json()
        if r.status_code == 200: return info
        raise Exception("Error fetching {}\ncode: {}\nreason: {}".format(
            r.url, info["code"], r.reason if info is None else info["message"]))

    def __fetch(self, action:str, pathName:str=None, pathVal:str=None, query:dict=None) -> tuple:
        (url, args) = self.__mkURL(action, pathName, pathVal, query)
        return self.__get(url, args)

    def stations(self, ids:str = None) -> dict:
        return self.__fetch("stations", "station-ids", ids)

    def nodes(self, ids:str = None) -> dict:
        return self.__fetch("nodes", "nodes-ids", ids)

    def sensors(self, ids:str = None) -> dict:
        return self.__fetch("sensors", "sensor-ids", ids)

    def sensorActivity(self, ids:str = None) -> dict:
        return self.__fetch("sensor-activity", "sensor-ids", ids)

    def sensorCatalog(self) -> dict:
        return self.__fetch("sensor-catalog")

    def current(self, ids:str = None) -> dict:
        return self.__fetch("current", "station-id", ids)

    def historic(self, ids:str=None, stime:int=None, etime:int=None) -> dict:
        query = {}
        if stime is not None: query["start-timestamp"] = stime
        if etime is not None: query["end-timestamp"] = etime
        return self.__fetch("historic", "station-id", ids, query)


if __name__ == "__main__":
    actions = ["stations", "nodes", "sensors", "sensor-activity", "sensor-catalog",
            "current", "historic"]
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="+", type=str, choices=actions,
            help="API action(s) to do")
    parser.add_argument("--id", type=str, help="comma deliminated list of id(s) for an action")
    parser.add_argument("--start", type=str, help="First date/time to fetch historic data for")
    parser.add_argument("--stop", type=int, help="Last date/time to fetch historic data for")
    V2.addArgs(parser)
    args = parser.parse_args()
    a = V2(args)
    for item in args.action:
        info = None
        if item == "stations":
            info = a.stations(args.id)
        elif item == "nodes":
            info = a.nodes(args.id)
        elif item == "sensors":
            info = a.sensors(args.id)
        elif item == "sensor-activity":
            info = a.sensorActivity(args.id)
        elif item == "sensor-catalog":
            info = a.sensorCatalog()
        elif item == "current":
            if args.id is None: parser.error("--id is required for current action")
            info = a.current(args.id)
        elif item == "historic":
            if args.id is None: parser.error("--id is required for historic action")
            if args.start is None: args.start = int(time.time()) - 3600
            if args.stop is None: args.stop = int(time.time())
            info = a.historic(args.id, args.start, args.stop)
        else:
            print("Unrecognized action, '{}'".format(item))
        if info is not None: 
            print("ACTION:", item)
            print(json.dumps(info, sort_keys=True, indent=2))
