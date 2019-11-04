import click
import configparser
import glob
import logging
import os
import sched
import signal
import sys
import time
import MySQLdb

from jog import JogFormatter
from prometheus_client import start_http_server, Gauge

from prometheus_mysql_exporter.parser import parse_response

CONTEXT_SETTINGS = {
    'help_option_names': ['-h', '--help']
}

gauges = {}


def format_label_value(value_list):
    return '_'.join(value_list)


def format_metric_name(name_list):
    return '_'.join(name_list)


def update_gauges(metrics):
    metric_dict = {}
    for (name_list, label_dict, value) in metrics:
        metric_name = format_metric_name(name_list)
        if metric_name not in metric_dict:
            metric_dict[metric_name] = (tuple(label_dict.keys()), {})

        label_keys = metric_dict[metric_name][0]
        label_values = tuple([
            format_label_value(label_dict[key])
            for key in label_keys
        ])

        metric_dict[metric_name][1][label_values] = value

    for metric_name, (label_keys, value_dict) in metric_dict.items():
        if metric_name in gauges:
            (old_label_values_set, gauge) = gauges[metric_name]
        else:
            old_label_values_set = set()
            gauge = Gauge(metric_name, '', label_keys)

        new_label_values_set = set(value_dict.keys())

        for label_values in old_label_values_set - new_label_values_set:
            gauge.remove(*label_values)

        for label_values, value in value_dict.items():
            if label_values:
                gauge.labels(*label_values).set(value)
            else:
                gauge.set(value)

        gauges[metric_name] = (new_label_values_set, gauge)


def run_scheduler(scheduler, mysql_client, dbs, name, interval, query, value_columns):
    def scheduled_run(scheduled_time):
        all_metrics = []

        for db in dbs:
            mysql_client.select_db(db)
            with mysql_client.cursor() as cursor:
                try:
                    cursor.execute(query)
                    raw_response = cursor.fetchall()

                    columns = [column[0] for column in cursor.description]
                    response = [{column: row[i] for i, column in enumerate(columns)} for row in raw_response]

                    metrics = parse_response(value_columns, response, [name], {'db': [db]})
                except Exception:
                    logging.exception('Error while querying db [%s], query [%s].', db, query)
                else:
                    all_metrics += metrics

        update_gauges(all_metrics)

        current_time = time.monotonic()
        next_scheduled_time = scheduled_time + interval
        while next_scheduled_time < current_time:
            next_scheduled_time += interval

        scheduler.enterabs(
            next_scheduled_time,
            1,
            scheduled_run,
            (next_scheduled_time,)
        )

    next_scheduled_time = time.monotonic()
    scheduler.enterabs(
        next_scheduled_time,
        1,
        scheduled_run,
        (next_scheduled_time,)
    )


def shutdown():
    logging.info('Shutting down')
    sys.exit(1)


def signal_handler(signum, frame):
    shutdown()


def validate_server_address(ctx, param, address_string):
    if ':' in address_string:
        host, port_string = address_string.split(':', 1)
        try:
            port = int(port_string)
        except ValueError:
            msg = "port '{}' in address '{}' is not an integer".format(port_string, address_string)
            raise click.BadParameter(msg)
        return (host, port)
    else:
        return (address_string, 3306)


@click.command(context_settings=CONTEXT_SETTINGS)
@click.option('--port', '-p', default=9207,
              help='Port to serve the metrics endpoint on. (default: 9207)')
@click.option('--config-file', '-c', default='exporter.cfg', type=click.File(),
              help='Path to query config file. '
                   'Can be absolute, or relative to the current working directory. '
                   '(default: exporter.cfg)')
@click.option('--config-dir', default='./config', type=click.Path(file_okay=False),
              help='Path to query config directory. '
                   'If present, any files ending in ".cfg" in the directory '
                   'will be parsed as additional query config files. '
                   'Merge order is main config file, then config directory files '
                   'in filename order. '
                   'Can be absolute, or relative to the current working directory. '
                   '(default: ./config)')
@click.option('--mysql-server', '-s', callback=validate_server_address, default='localhost',
              help='Address of a MySQL server to run queries on. '
                   'A port can be provided if non-standard (3306) e.g. mysql:3333. '
                   '(default: localhost)')
@click.option('--mysql-databases', '-d', required=True,
              help='Databases to run queries on. '
                   'Database names should be separated by commas e.g. db1,db2.')
@click.option('--mysql-user', '-u', default='root',
              help='MySQL user to run queries as. (default: root)')
@click.option('--mysql-password', '-P', default='',
              help='Password for the MySQL user, if required. (default: no password)')
@click.option('--json-logging', '-j', default=False, is_flag=True,
              help='Turn on json logging.')
@click.option('--log-level', default='INFO',
              type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']),
              help='Detail level to log. (default: INFO)')
@click.option('--verbose', '-v', default=False, is_flag=True,
              help='Turn on verbose (DEBUG) logging. Overrides --log-level.')
def cli(**options):
    """Export MySQL query results to Prometheus."""

    signal.signal(signal.SIGTERM, signal_handler)

    log_handler = logging.StreamHandler()
    log_format = '[%(asctime)s] %(name)s.%(levelname)s %(threadName)s %(message)s'
    formatter = JogFormatter(log_format) if options['json_logging'] else logging.Formatter(log_format)
    log_handler.setFormatter(formatter)

    log_level = getattr(logging, options['log_level'])
    logging.basicConfig(
        handlers=[log_handler],
        level=logging.DEBUG if options['verbose'] else log_level
    )
    logging.captureWarnings(True)

    port = options['port']
    mysql_host, mysql_port = options['mysql_server']

    dbs = options['mysql_databases'].split(',')

    username = options['mysql_user']
    password = options['mysql_password']

    config = configparser.ConfigParser()
    config.read_file(options['config_file'])

    config_dir_file_pattern = os.path.join(options['config_dir'], '*.cfg')
    config_dir_sorted_files = sorted(glob.glob(config_dir_file_pattern))
    config.read(config_dir_sorted_files)

    query_prefix = 'query_'
    queries = {}
    for section in config.sections():
        if section.startswith(query_prefix):
            query_name = section[len(query_prefix):]
            query_interval = config.getfloat(section, 'QueryIntervalSecs')
            query = config.get(section, 'QueryStatement')
            value_columns = config.get(section, 'QueryValueColumns').split(',')

            queries[query_name] = (query_interval, query, value_columns)

    scheduler = sched.scheduler()

    for name, (interval, query, value_columns) in queries.items():
        mysql_client = MySQLdb.connect(host=mysql_host,
                                       port=mysql_port,
                                       user=username,
                                       passwd=password,
                                       autocommit=True)
        run_scheduler(scheduler, mysql_client, dbs, name, interval, query, value_columns)

    logging.info('Starting server...')
    start_http_server(port)
    logging.info('Server started on port %s', port)

    try:
        scheduler.run()
    except KeyboardInterrupt:
        pass

    shutdown()


def main():
    cli(auto_envvar_prefix='MYSQL_EXPORTER')
