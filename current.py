#! /usr/bin/env python3
#
# Fetch current sensor readings and store them into an SQLite3 database
#
# Jan-2020, Pat Welch, pat@mousebrains.com

import WeatherLink
import argparse
import time
import json
import sqlite3

class DB:
    def __init__(self, args:argparse.ArgumentParser) -> None:
        self.__fn = args.db
        self.__db = None
        self.__ids = set()

    @staticmethod
    def addArgs(parser:argparse.ArgumentParser) -> argparse.ArgumentParser:
        parser.add_argument("--db", type=str, help="SQLite3 database to store results in")
        return parser

    def __delete(self) -> None:
        self.close()

    def __exec(self, sql:str, args:tuple=None) -> None:
        if self.__fn is None:
            print(sql, args)
            return

        if self.__db is None:
            self.__db = sqlite3.connect(self.__fn)
        if args is None:
            self.__db.execute(sql)
        else:
            self.__db.execute(sql, args)

    def begin(self): self.__exec("BEGIN TRANSACTION;")
    def commit(self): self.__exec("COMMIT;")
    def rollback(self): self.__exec("ROLLBACK;")
    def close(self): 
        if self.__db is not None:
            self.__db.close()
            self.__db = None

    def station(self, row:dict) -> str: return self.__insertRow("station", "station_id", row)
    def sensor(self, row:dict) -> str: return self.__insertRow("sensor", "lsid", row)

    def __insertRow(self, tbl:str, idKey:str, row:dict) -> str:
        if idKey not in row:
            raise Exception("{} not found in\n{}".format(
                idKey, 
                json.dumps(row, sort_keys=True, indent=2)))

        sql = "CREATE TABLE IF NOT EXISTS " + tbl + "("
        sql+= "  " + idKey + " INTEGER PRIMARY KEY,"
        cols = []
        vals = [row[idKey]]
        for key in sorted(row):
            if key != idKey:
                cols.append(key)
                vals.append(row[key])
        sql += ",\n  ".join(cols) + ");"
        self.__exec(sql)
        
        sql = "INSERT OR REPLACE INTO "
        sql+= tbl
        sql+= " VALUES(" + ",".join(["?"]*len(vals)) + ");"
        self.__exec(sql, vals)
        return row[idKey]

    def mkObsTable(self) -> None:
        sql = "CREATE TABLE IF NOT EXISTS obs("
        sql+= "  stationID INTEGER,"
        sql+= "  sensorID INTEGER,"
        sql+= "  t INTEGER,"
        sql+= "  name TEXT,"
        sql+= "  value REAL,"
        sql+= "  FOREIGN KEY(stationID) references station(station_id) ON DELETE CASCADE,"
        sql+= "  FOREIGN KEY(sensorID) references sensor(lsid) ON DELETE CASCADE,"
        sql+= "  PRIMARY KEY(stationID, sensorID, name, t)"
        sql+= ");"
        self.__exec(sql)

    def observation(self, stationID:int, sensorID:int, t:int, name:str, obs) -> None:
        sql = "INSERT OR REPLACE INTO obs VALUES(?,?,?,?,?);"
        self.__exec(sql, (stationID, sensorID, t, name, obs))


parser = argparse.ArgumentParser()
parser.add_argument("--id", type=str, help="comma deliminated list of id(s) for an action")
parser.add_argument("--dt", type=float, help="Seconds between fetching data")
DB.addArgs(parser)
WeatherLink.V2.addArgs(parser)
args = parser.parse_args()
a = WeatherLink.V2(args)
db = DB(args)

stationIDs = set()
info = a.stations(args.id)
db.begin()
for row in info["stations"]: stationIDs.add(db.station(row))
db.commit()

info = a.sensors()
db.begin()
for row in info["sensors"]: db.sensor(row)
db.commit()

db.begin()
db.mkObsTable()
db.commit()

while True:
    db.begin()
    for stationID in stationIDs:
        info = a.current(str(stationID))
        for sensor in info["sensors"]:
            if "lsid" not in sensor: raise Exeception("lsid not found in {}".format(sensor))
            if "data" not in sensor: raise Exeception("data not found in {}".format(sensor))
            lsid = sensor["lsid"]
            data = sensor["data"]
            for row in data:
                if "ts" not in row:
                    raise Exception("ts not found in {}".format(row))
                t = row["ts"]
                for obs in row:
                    if obs == "ts": continue
                    db.observation(stationID, lsid, t, obs, row[obs])
    db.commit()
    if args.dt is None: break
    time.sleep(args.dt)

db.close()

print("IDs", stationIDs)
