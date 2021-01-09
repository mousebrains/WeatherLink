# WeatherLink Version 2 API

A Python3 interface to the 
[WeatherLink V2 API.](https://weatherlink.github.io/v2-api)

You must have a
[YAML](https://docs.ansible.com/ansible/latest/reference_appendices/YAMLSyntax.html)
config file which contains two rows:

*secret: "1234"* # Replace 1234 with your WeatherLink API secret\
*key: "1234"* # Replace 1234 with your WeatherLink API key

By default this file is located in *~/.weatherlink/config.v2.yml*

<code>WeatherLink.py</code>
is a standalone program able to fetch information via the 
[WeatherLink V2 API.](https://weatherlink.github.io/v2-api)
Typical usage can be:

<code>./WeatherLink.py stations</code>

which will print out the 
[JSON](https://www.json.org/json-en.html)
returned by the call.

<code>current.py</code>
fetches all the current observation for all the stations associated with your key/secret, then stores the results in an 
[SQLite3](https://www.sqlite.org/index.html)
database.

The 
[SQLite3](https://www.sqlite.org/index.html)
database schema is:

This software is tested with
[Python 3.9](https://www.python.org)
on a
[MacOS 11.1 system.](https://developer.apple.com/documentation/macos-release-notes)
