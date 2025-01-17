import argparse
import json
import logging
import os
import re

from io import BytesIO
from tempfile import SpooledTemporaryFile, TemporaryFile

import boto3
import botocore
import requests
from metadict import MetaDict
from warcio.archiveiterator import ArchiveIterator
from warcio.recordloader import ArchiveLoadFailed

from pyspark.sql import SparkSession
from pyspark.sql.types import StructType, StructField, StringType, LongType


LOGGING_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'


class CCSparkJob(object):
    """
    A simple Spark job definition to process Common Crawl data
    """

    name = 'CCSparkJob'

    output_schema = StructType([
        StructField("key", StringType(), True),
        StructField("val", LongType(), True)
    ])

    # description of input and output shown by --help
    input_descr = "Path to file listing input paths"
    output_descr = "Name of output table (saved in spark.sql.warehouse.dir)"

    # parse HTTP headers of WARC records (derived classes may override this)
    warc_parse_http_header = True

    args = None
    records_processed = None
    warc_input_processed = None
    warc_input_failed = None
    log_level = 'INFO'
    logging.basicConfig(level=log_level, format=LOGGING_FORMAT)

    num_input_partitions = 400
    num_output_partitions = 10

    # S3 client is thread-safe, cf.
    # https://boto3.amazonaws.com/v1/documentation/api/latest/guide/clients.html#multithreading-or-multiprocessing-with-clients)
    s3client = None

    # pattern to split a data URL (<scheme>://<netloc>/<path> or <scheme>:/<path>)
    data_url_pattern = re.compile('^(s3|https?|file|hdfs):(?://([^/]*))?/(.*)')


    def set_settings(self):
        """Returns the parsed arguments from the command line"""

        dict_config = {
            'input_base_url': 'https://data.commoncrawl.org/',
            'num_input_partitions': self.num_input_partitions,
            'num_output_partitions': self.num_input_partitions,
            'output_format': "parquet",
            'output_compression': "gzip",
            'output_option': [],
            'local_temp_dir': None,
            'log_level': self.log_level,
            'spark_profiler': True,

        }
        args = MetaDict(dict_config)
        self.init_logging(args.log_level)
        return args

    def add_arguments(self, parser):
        """Allows derived classes to add command-line arguments.
           Derived classes overriding this method must call
           super().add_arguments(parser) in order to add "register"
           arguments from all classes in the hierarchy."""
        pass

    def validate_arguments(self, args):
        """Validate arguments. Derived classes overriding this method
           must call super().validate_arguments(args)."""
        if "orc" == args.output_format and "gzip" == args.output_compression:
            # gzip for Parquet, zlib for ORC
            args.output_compression = "zlib"
        return True

    def get_output_options(self):
        """Convert output options strings (opt=val) to kwargs"""
        return {x[0]: x[1] for x in map(lambda x: x.split('=', 1),
                                        self.args.output_option)}

    def init_logging(self, level=None, session=None):
        if level:
            self.log_level = level
        else:
            level = self.log_level
        logging.basicConfig(level=level, format=LOGGING_FORMAT)
        logging.getLogger(self.name).setLevel(level)
        if session:
            session.sparkContext.setLogLevel(level)

    def run(self, input_paths_file_path, output_table):
        """Run the job"""
        self.args = self.set_settings()
        self.args.input_paths_file_path = input_paths_file_path
        self.args.output_table = output_table

        builder = SparkSession.builder.appName(self.name)

        """
        if self.args.spark_profiler:
            builder.config("spark.python.profile", "true")
        """
        session = builder.getOrCreate()
        self.run_job(session)

        """
        if self.args.spark_profiler:
            session.sparkContext.show_profiles()
        """
        session.stop()

    @staticmethod
    def reduce_by_key_func(a, b):
        return a + b

    def run_job(self, session):
        input_data = session.sparkContext.textFile(self.args.input_paths_file_path,
                                                   minPartitions=self.args.num_input_partitions)

        output = input_data.mapPartitionsWithIndex(self.process_warcs) \
            .reduceByKey(self.reduce_by_key_func)

        session.createDataFrame(output, schema=self.output_schema) \
            .coalesce(self.args.num_output_partitions) \
            .write \
            .format(self.args.output_format) \
            .option("compression", self.args.output_compression) \
            .options(**self.get_output_options()) \
            .saveAsTable(self.args.output_table)

    def fetch_warc(self, uri, base_uri=None, offset=-1, length=-1):
        """Fetch WARC/WAT/WET files (or a record if offset and length are given)"""

        (scheme, netloc, path) = (None, None, None)
        uri_match = self.data_url_pattern.match(uri)
        if not uri_match and base_uri:
            # relative input URI (path) and base URI defined
            uri = base_uri + uri
            uri_match = self.data_url_pattern.match(uri)

        if uri_match:
            (scheme, netloc, path) = uri_match.groups()
        else:
            # keep local file paths as is
            path = uri

        stream = None

        if scheme == 'http' or scheme == 'https':
            headers = None
            if offset > -1 and length > 0:
                headers = {
                    "Range": "bytes={}-{}".format(offset, (offset + length - 1))
                }
                # Note: avoid logging many small fetches
                #self.get_logger().debug('Fetching {} ({})'.format(uri, headers))

            response = requests.get(uri, headers=headers)

            if response.ok:
                # includes "HTTP 206 Partial Content" for range requests
                warctemp = SpooledTemporaryFile(max_size=2097152,
                                                mode='w+b',
                                                dir=self.args.local_temp_dir)
                warctemp.write(response.content)
                warctemp.seek(0)
                stream = warctemp
        return stream

    def process_warcs(self, _id, iterator):
        """Process WARC/WAT/WET files, calling iterate_records(...) for each file"""
        for uri in iterator:
            self.warc_input_processed.add(1)

            stream = self.fetch_warc(uri, self.args.input_base_url)
            if not stream:
                continue

            no_parse = (not self.warc_parse_http_header)
            try:
                archive_iterator = ArchiveIterator(stream,
                                                   no_record_parse=no_parse, arc2warc=True)
                for res in self.iterate_records(uri, archive_iterator):
                    yield res
            finally:
                stream.close()

    def process_record(self, record):
        """Process a single WARC/WAT/WET record"""
        raise NotImplementedError('Processing record needs to be customized')

    def iterate_records(self, _warc_uri, archive_iterator):
        """Iterate over all WARC records. This method can be customized
           and allows to access also values from ArchiveIterator, namely
           WARC record offset and length."""
        for record in archive_iterator:
            for res in self.process_record(record):
                yield res
            self.records_processed.add(1)
            # WARC record offset and length should be read after the record
            # has been processed, otherwise the record content is consumed
            # while offset and length are determined:
            #  warc_record_offset = archive_iterator.get_record_offset()
            #  warc_record_length = archive_iterator.get_record_length()

    @staticmethod
    def is_wet_text_record(record):
        """Return true if WARC record is a WET text/plain record"""
        return (record.rec_type == 'conversion' and
                record.content_type == 'text/plain')

    @staticmethod
    def is_wat_json_record(record):
        """Return true if WARC record is a WAT record"""
        return (record.rec_type == 'metadata' and
                record.content_type == 'application/json')

    @staticmethod
    def is_html(record):
        """Return true if (detected) MIME type of a record is HTML"""
        html_types = ['text/html', 'application/xhtml+xml']
        if (('WARC-Identified-Payload-Type' in record.rec_headers) and
            (record.rec_headers['WARC-Identified-Payload-Type'] in
             html_types)):
            return True
        content_type = record.http_headers.get_header('content-type', None)
        if content_type:
            for html_type in html_types:
                if html_type in content_type:
                    return True
        return False


class CCIndexSparkJob(CCSparkJob):
    """
    Process the Common Crawl columnar URL index
    """

    name = "CCIndexSparkJob"

    # description of input and output shown in --help
    input_descr = "Path to Common Crawl index table"

    def add_arguments(self, parser):
        parser.add_argument("--table", default="ccindex",
                            help="name of the table data is loaded into"
                            " (default: ccindex)")
        parser.add_argument("--query", default=None, required=True,
                            help="SQL query to select rows (required).")
        parser.add_argument("--table_schema", default=None,
                            help="JSON schema of the ccindex table,"
                            " implied from Parquet files if not provided.")

    def load_table(self, session, table_path, table_name):
        parquet_reader = session.read.format('parquet')
        if self.args.table_schema is not None:
            with open(self.args.table_schema, 'r') as s:
                schema = StructType.fromJson(json.loads(s.read()))
            parquet_reader = parquet_reader.schema(schema)
        df = parquet_reader.load(table_path)
        df.createOrReplaceTempView(table_name)


    def execute_query(self, session, query):
        sqldf = session.sql(query)
        sqldf.explain()
        return sqldf

    def load_dataframe(self, session, partitions=-1):
        self.load_table(session, self.args.input_paths_file_path, self.args.table)
        sqldf = self.execute_query(session, self.args.query)
        sqldf.persist()
        num_rows = sqldf.count()
        if partitions > 0:
            sqldf = sqldf.repartition(partitions)
            sqldf.persist()

        return sqldf

    def run_job(self, session):
        sqldf = self.load_dataframe(session, self.args.num_output_partitions)

        sqldf.write \
            .format(self.args.output_format) \
            .option("compression", self.args.output_compression) \
            .options(**self.get_output_options()) \
            .saveAsTable(self.args.output_table)



class CCIndexWarcSparkJob(CCIndexSparkJob):
    """
    Process Common Crawl data (WARC records) found by the columnar URL index
    """

    name = "CCIndexWarcSparkJob"

    input_descr = "Path to Common Crawl index table (with option `--query`)" \
                  " or extracted table containing WARC record coordinates"

    def add_arguments(self, parser):
        super(CCIndexWarcSparkJob, self).add_arguments(parser)
        agroup = parser.add_mutually_exclusive_group(required=True)
        agroup.add_argument("--query", default=None,
                            help="SQL query to select rows. Note: the result "
                            "is required to contain the columns `url', `warc"
                            "_filename', `warc_record_offset' and `warc_record"
                            "_length', make sure they're SELECTed. The column "
                            "`content_charset' is optional and is utilized to "
                            "read WARC record payloads with the right encoding.")
        agroup.add_argument("--csv", default=None,
                            help="CSV file to load WARC records by filename, "
                            "offset and length. The CSV file must have column "
                            "headers and the input columns `url', "
                            "`warc_filename', `warc_record_offset' and "
                            "`warc_record_length' are mandatory, see also "
                            "option --query.\nDeprecated, use instead "
                            "`--input_table_format csv` together with "
                            "`--input_table_option header=True` and "
                            "`--input_table_option inferSchema=True`.")
        agroup.add_argument("--input_table_format", default=None,
                            help="Data format of the input table to load WARC "
                            "records by filename, offset and length. The input "
                            "table is read from the path <input> and is expected "
                            "to include the columns `url', `warc_filename', "
                            "`warc_record_offset' and `warc_record_length'. The "
                            "input table is typically a result of a CTAS query "
                            "(create table as).  Allowed formats are: orc, "
                            "json lines, csv, parquet and other formats "
                            "supported by Spark.")
        parser.add_argument("--input_table_option", action='append', default=[],
                            help="Additional input option when reading data from "
                            "an input table (see `--input_table_format`). Options "
                            "are passed to the Spark DataFrameReader.")

    def get_input_table_options(self):
        return {x[0]: x[1] for x in map(lambda x: x.split('=', 1),
                                        self.args.input_table_option)}

    def load_dataframe(self, session, partitions=-1):
        if self.args.query is not None:
            return super(CCIndexWarcSparkJob, self).load_dataframe(session, partitions)

        if self.args.csv is not None:
            sqldf = session.read.format("csv").option("header", True) \
                .option("inferSchema", True).load(self.args.csv)
        elif self.args.input_table_format is not None:
            data_format = self.args.input_table_format
            reader = session.read.format(data_format)
            reader = reader.options(**self.get_input_table_options())
            sqldf = reader.load(self.args.input_paths_file_path)

        if partitions > 0:

            sqldf = sqldf.repartition(partitions)

        sqldf.persist()

        return sqldf

    def process_record_with_row(self, record, row):
        """Process a single WARC record and the corresponding table row."""
        if 'content_charset' in row:
            # pass `content_charset` forward to subclass processing WARC records
            record.rec_headers['WARC-Identified-Content-Charset'] = row['content_charset']
        for res in self.process_record(record):
            yield res

    def fetch_process_warc_records(self, rows):
        """Fetch and process WARC records specified by columns warc_filename, 
        warc_record_offset and warc_record_length in rows"""

        no_parse = (not self.warc_parse_http_header)

        for row in rows:
            url = row['url']
            warc_path = row['warc_filename']
            offset = int(row['warc_record_offset'])
            length = int(row['warc_record_length'])
            record_stream = self.fetch_warc(warc_path, self.args.input_base_url, offset, length)
            try:
                for record in ArchiveIterator(record_stream,
                                              no_record_parse=no_parse):
                    for res in self.process_record_with_row(record, row):
                        yield res
                    self.records_processed.add(1)
            except ArchiveLoadFailed as exception:
                self.warc_input_failed.add(1)

    def run_job(self, session):
        sqldf = self.load_dataframe(session, self.args.num_input_partitions)

        columns = ['url', 'warc_filename', 'warc_record_offset', 'warc_record_length']
        if 'content_charset' in sqldf.columns:
            columns.append('content_charset')
        warc_recs = sqldf.select(*columns).rdd

        output = warc_recs.mapPartitions(self.fetch_process_warc_records) \
            .reduceByKey(self.reduce_by_key_func)

        session.createDataFrame(output, schema=self.output_schema) \
            .coalesce(self.args.num_output_partitions) \
            .write \
            .format(self.args.output_format) \
            .option("compression", self.args.output_compression) \
            .options(**self.get_output_options()) \
            .saveAsTable(self.args.output_table)
