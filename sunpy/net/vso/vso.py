# -*- coding: utf-8 -*-
# Author: Florian Mayer <florian.mayer@bitsrc.org>
#
# This module was developed with funding provided by
# the ESA Summer of Code (2011).
#
# pylint: disable=W0401,C0103,R0904,W0141
from __future__ import absolute_import, division, print_function

"""
This module provides a wrapper around the VSO API.
"""

import re
import os
import sys
import logging
import threading

from datetime import datetime, timedelta
from functools import partial
from collections import defaultdict
from suds import client, TypeNotFound

import astropy
from astropy.table import Table, Column
import astropy.units as u

from sunpy import config
from sunpy.net import download
from sunpy.net.proxyfix import WellBehavedHttpTransport
from sunpy.util.progressbar import TTYProgressBar as ProgressBar
from sunpy.util.net import get_filename, slugify
from sunpy.net.attr import and_, Attr
from sunpy.net.vso import attrs
from sunpy.net.vso.attrs import walker, TIMEFORMAT
from sunpy.util import replacement_filename
from sunpy.time import parse_time

from sunpy.extern import six
from sunpy.extern.six import iteritems, text_type
from sunpy.extern.six.moves import input

TIME_FORMAT = config.get("general", "time_format")

DEFAULT_URL = 'http://docs.virtualsolar.org/WSDL/VSOi_rpc_literal.wsdl'
DEFAULT_PORT = 'nsoVSOi'
RANGE = re.compile(r'(\d+)(\s*-\s*(\d+))?(\s*([a-zA-Z]+))?')

# Override the logger that dumps the whole Schema
# to stderr so it doesn't do that.
suds_log = logging.getLogger('suds.umx.typed')
suds_log.setLevel(50)


# TODO: Name
class NoData(Exception):
    """ Risen for callbacks of VSOClient that are unable to supply
    information for the request. """
    pass


class _Str(str):
    """ Subclass of string that contains a meta attribute for the
    record_item associated with the file. """
    pass


# ----------------------------------------

def _parse_waverange(string):
    min_, max_, unit = RANGE.match(string).groups()[::2]
    return {
        'wave_wavemin': min_,
        'wave_wavemax': min_ if max_ is None else max_,
        'wave_waveunit': 'Angstrom' if unit is None else unit,
    }


def _parse_date(string):
    start, end = string.split(' - ')
    return {'time_start': start.strip(), 'time_end': end.strip()}


def iter_records(response):
    for prov_item in response.provideritem:
        if not hasattr(prov_item, 'record') or not prov_item.record:
            continue
        for record_item in prov_item.record.recorditem:
            yield record_item


def iter_errors(response):
    for prov_item in response.provideritem:
        if not hasattr(prov_item, 'record') or not prov_item.record:
            yield prov_item


# TODO: Python 3 this should subclass from UserList
class QueryResponse(list):
    def __init__(self, lst, queryresult=None, table=None):
        super(QueryResponse, self).__init__(lst)
        self.queryresult = queryresult
        self.errors = []
        self.table = None

    def query(self, *query):
        """ Furtherly reduce the query response by matching it against
        another query, e.g. response.query(attrs.Instrument('aia')). """
        query = and_(*query)
        return QueryResponse(
            attrs.filter_results(query, self), self.queryresult
        )

    @classmethod
    def create(cls, queryresult):
        return cls(iter_records(queryresult), queryresult)

    def total_size(self):
        """ Total size of data in KB. May be less than the actual
        size because of inaccurate data providers. """
        # Warn about -1 values?
        return sum(record.size for record in self if record.size > 0)

    def time_range(self):
        """ Return total time-range all records span across. """
        return (
            datetime.strptime(
                min(record.time.start for record in self
                  if record.time.start is not None), TIMEFORMAT),
            datetime.strptime(
                max(record.time.end for record in self
                  if record.time.end is not None), TIMEFORMAT)
        )

    def build_table(self):
        keywords = ['Start Time', 'End Time', 'Source', 'Instrument', 'Type', 'Wavelength']
        record_items = {}
        for key in keywords:
            record_items[key] = []

        def validate_time(time):
            # Handle if the time is None when coming back from VSO
            if time is None:
                return ['None']
            if record.time.start is not None:
                return [datetime.strftime(parse_time(time), TIME_FORMAT)]
            else:
                return ['N/A']

        for record in self:
            record_items['Start Time'].append(validate_time(record.time.start))
            record_items['End Time'].append(validate_time(record.time.end))
            record_items['Source'].append(str(record.source))
            record_items['Instrument'].append(str(record.instrument))
            record_items['Type'].append(str(record.extent.type)
                                if record.extent.type is not None else ['N/A'])
            # If we have a start and end Wavelength, make a quantity
            if hasattr(record, 'wave') and record.wave.wavemin and record.wave.wavemax:
                record_items['Wavelength'].append(u.Quantity([float(record.wave.wavemin),
                                                              float(record.wave.wavemax)],
                                                             unit=record.wave.waveunit))
            # If not save None
            else:
                record_items['Wavelength'].append(None)
        # If we have no wavelengths for the whole list, drop the col
        if all([a is None for a in record_items['Wavelength']]):
            record_items.pop('Wavelength')
            keywords.remove('Wavelength')
        else:
            # Make whole column a quantity
            try:
                with u.set_enabled_equivalencies(u.spectral()):
                    record_items['Wavelength'] = u.Quantity(record_items['Wavelength'])
            # If we have mixed units or some Nones just represent as strings
            except (u.UnitConversionError, TypeError):
                record_items['Wavelength'] = [str(a) for a in record_items['Wavelength']]

        return Table(record_items)[keywords]

    def add_error(self, exception):
        self.errors.append(exception)

    def __str__(self):
        """Print out human-readable summary of records retrieved"""
        return str(self.build_table())

    def __repr__(self):
        """Print out human-readable summary of records retrieved"""
        return repr(self.build_table())

    def _repr_html_(self):
        return self.build_table()._repr_html_()


class DownloadFailed(Exception):
    pass

class MissingInformation(Exception):
    pass

class UnknownMethod(Exception):
    pass

class MultipleChoices(Exception):
    pass

class UnknownVersion(Exception):
    pass

class UnknownStatus(Exception):
    pass

class VSOClient(object):
    """ Main VSO Client. """
    method_order = [
        'URL-TAR_GZ', 'URL-ZIP', 'URL-TAR', 'URL-FILE', 'URL-packaged'
    ]

    def __init__(self, url=None, port=None, api=None):
        if api is None:
            if url is None:
                url = DEFAULT_URL
            if port is None:
                port = DEFAULT_PORT

            api = client.Client(url, transport=WellBehavedHttpTransport())
            api.set_options(port=port)
        self.api = api

    def make(self, atype, **kwargs):
        """ Create new SOAP object with attributes specified in kwargs.
        To assign subattributes, use foo__bar=1 to assign
        ['foo']['bar'] = 1. """
        obj = self.api.factory.create(atype)
        for k, v in iteritems(kwargs):
            split = k.split('__')
            tip = split[-1]
            rest = split[:-1]

            item = obj
            for elem in rest:
                item = item[elem]

            if isinstance(v, dict):
                # Do not throw away type information for dicts.
                for k, v in iteritems(v):
                    item[tip][k] = v
            else:
                item[tip] = v
        return obj

    def query(self, *query):
        """ Query data from the VSO with the new API. Takes a variable number
        of attributes as parameter, which are chained together using AND.

        The new query language allows complex queries to be easily formed.

        Examples
        --------
        Query all data from eit or aia between 2010-01-01T00:00 and
        2010-01-01T01:00.

        >>> from datetime import datetime
        >>> from sunpy.net import vso
        >>> client = vso.VSOClient()
        >>> client.query(
        ...    vso.attrs.Time(datetime(2010, 1, 1), datetime(2010, 1, 1, 1)),
        ...    vso.attrs.Instrument('eit') | vso.attrs.Instrument('aia'))   # doctest: +NORMALIZE_WHITESPACE
        <Table masked=False length=5>
           Start Time [1]       End Time [1]     Source  Instrument   Type
             string152           string152      string32  string24  string64
        ------------------- ------------------- -------- ---------- --------
        2010-01-01 00:00:08 2010-01-01 00:00:20     SOHO        EIT FULLDISK
        2010-01-01 00:12:08 2010-01-01 00:12:20     SOHO        EIT FULLDISK
        2010-01-01 00:24:10 2010-01-01 00:24:22     SOHO        EIT FULLDISK
        2010-01-01 00:36:08 2010-01-01 00:36:20     SOHO        EIT FULLDISK
        2010-01-01 00:48:09 2010-01-01 00:48:21     SOHO        EIT FULLDISK

        Returns
        -------
        out : :py:class:`QueryResult` (enhanced list) of matched items. Return
        value of same type as the one of :py:meth:`VSOClient.query`.
        """
        query = and_(*query)

        responses = []
        for block in walker.create(query, self.api):
            try:
                responses.append(
                    self.api.service.Query(
                        self.make('QueryRequest', block=block)
                    )
                )
            except TypeNotFound:
                pass
            except Exception as ex:
                response = QueryResponse.create(self.merge(responses))
                response.add_error(ex)

        return QueryResponse.create(self.merge(responses))

    def merge(self, queryresponses):
        """ Merge responses into one. """
        if len(queryresponses) == 1:
            return queryresponses[0]

        fileids = set()
        providers = {}

        for queryresponse in queryresponses:
            for provideritem in queryresponse.provideritem:
                provider = provideritem.provider
                if not hasattr(provideritem, 'record'):
                    continue
                if not hasattr(provideritem.record, 'recorditem'):
                    continue
                if provideritem.provider not in providers:
                    providers[provider] = provideritem
                    fileids |= set(
                        record_item.fileid
                        for record_item in provideritem.record.recorditem
                    )
                else:
                    for record_item in provideritem.record.recorditem:
                        if record_item.fileid not in fileids:
                            fileids.add(record_item.fileid)
                            providers[provider].record.recorditem.append(
                                record_item
                            )
                            providers[provider].no_of_records_found += 1
                            providers[provider].no_of_records_returned += 1
        return self.make('QueryResponse',
                         provideritem=list(providers.values()))

    @staticmethod
    def mk_filename(pattern, response, sock, url, overwrite=False):
        name = get_filename(sock, url)
        if not name:
            if not isinstance(response.fileid, text_type):
                name = six.u(response.fileid, "ascii", "ignore")
            else:
                name = response.fileid

        fs_encoding = sys.getfilesystemencoding()
        if fs_encoding is None:
            fs_encoding = "ascii"

        name = slugify(name)

        if six.PY2:
            name = name.encode(fs_encoding, "ignore")

        if not name:
            name = "file"

        fname = pattern.format(file=name, **dict(response))

        if not overwrite and os.path.exists(fname):
            fname = replacement_filename(fname)

        dir_ = os.path.abspath(os.path.dirname(fname))
        if not os.path.exists(dir_):
            os.makedirs(dir_)
        return fname

    # pylint: disable=R0914
    def query_legacy(self, tstart=None, tend=None, **kwargs):
        """
        Query data from the VSO mocking the IDL API as close as possible.
        Either tstart and tend or date_start and date_end or date have
        to be supplied.

        Parameters
        ----------
        tstart : datetime.datetime
            Start of the time-range in which records are searched.
        tend : datetime.datetime
            Start of the time-range in which records are searched.
        date : str
            (start date) - (end date)
        start_date : datetime
            the start date
        end_date : datetime
            the end date
        wave : str
            (min) - (max) (unit)
        min_wave : str
            minimum spectral range
        max_wave : str
            maximum spectral range
        unit_wave : str
            spectral range units (Angstrom, GHz, keV)
        extent : str
            VSO 'extent type' ... (FULLDISK, CORONA, LIMB, etc)
        physobj : str
            VSO 'physical observable'
        provider : str
            VSO ID for the data provider (SDAC, NSO, SHA, MSU, etc)
        source : str
            spacecraft or observatory (SOHO, YOHKOH, BBSO, etc)
            synonyms : spacecraft, observatory
        instrument : str
            instrument ID (EIT, SXI-0, SXT, etc)
            synonyms : telescope, inst
        detector : str
            detector ID (C3, EUVI, COR2, etc.)
        layout : str
            layout of the data (image, spectrum, time_series, etc.)

        level : str
            level of the data product (numeric range, see below)
        pixels : str
            number of pixels (numeric range, see below)
        resolution : str
            effective resolution (1 = full, 0.5 = 2x2 binned, etc)
            numeric range, see below.
        pscale : str
            pixel scale, in arcseconds (numeric range, see below)
        near_time : datetime
            return record closest to the time.  See below.
        sample : int
            attempt to return only one record per SAMPLE seconds.  See below.

        Numeric Ranges:

            - May be entered as a string or any numeric type for equality matching
            - May be a string of the format '(min) - (max)' for range matching
            - May be a string of the form '(operator) (number)' where operator is one of: lt gt le ge < > <= >=


        Examples
        --------
        Query all data from eit between 2010-01-01T00:00 and
        2010-01-01T01:00.

        >>> from datetime import datetime
        >>> from sunpy.net import vso
        >>> client = vso.VSOClient()
        >>> qr = client.query_legacy(datetime(2010, 1, 1),
        ...                          datetime(2010, 1, 1, 1), instrument='eit')

        Returns
        -------
        out : :py:class:`QueryResult` (enhanced list) of matched items. Return value of same type as the one of :py:class:`VSOClient.query`.
        """
        sdk = lambda key: lambda value: {key: value}
        ALIASES = {
            'wave_min': sdk('wave_wavemin'),
            'wave_max': sdk('wave_wavemax'),
            'wave_type': sdk('wave_wavetype'),
            'wave_unit': sdk('wave_waveunit'),
            'min_wave': sdk('wave_wavemin'),
            'max_wave': sdk('wave_wavemax'),
            'type_wave': sdk('wave_wavetype'),
            'unit_wave': sdk('wave_waveunit'),
            'wave': _parse_waverange,
            'inst': sdk('instrument'),
            'telescope': sdk('instrument'),
            'spacecraft': sdk('source'),
            'observatory': sdk('source'),
            'start_date': sdk('time_start'),
            'end_date': sdk('time_end'),
            'start': sdk('time_start'),
            'end': sdk('time_end'),
            'near_time': sdk('time_near'),
            'date': _parse_date,
            'layout': sdk('datatype'),
        }
        if tstart is not None:
            kwargs.update({'time_start': tstart})
        if tend is not None:
            kwargs.update({'time_end': tend})

        queryreq = self.api.factory.create('QueryRequest')
        for key, value in iteritems(kwargs):
            for k, v in iteritems(ALIASES.get(key, sdk(key))(value)):
                if k.startswith('time'):
                    v = parse_time(v).strftime(TIMEFORMAT)
                attr = k.split('_')
                lst = attr[-1]
                rest = attr[:-1]

                # pylint: disable=E1103
                item = queryreq.block
                for elem in rest:
                    try:
                        item = item[elem]
                    except KeyError:
                        raise ValueError("Unexpected argument {key!s}.".format(key=key))
                if lst not in item:
                    raise ValueError("Unexpected argument {key!s}.".format(key=key))
                if item[lst]:
                    raise ValueError("Got multiple values for {k!s}.".format(k=k))
                item[lst] = v
        try:
            return QueryResponse.create(self.api.service.Query(queryreq))
        except TypeNotFound:
            return QueryResponse([])

    def latest(self):
        """ Return newest record (limited to last week). """
        return self.query_legacy(
            datetime.utcnow()  - timedelta(7),
            datetime.utcnow(),
            time_near=datetime.utcnow()
        )

    def get(self, query_response, path=None, methods=('URL-FILE_Rice', 'URL-FILE'),
            downloader=None, site=None):
        """
        Download data specified in the query_response.

        Parameters
        ----------
        query_response : sunpy.net.vso.QueryResponse
            QueryResponse containing the items to be downloaded.

        path : str
            Specify where the data is to be downloaded. Can refer to arbitrary
            fields of the QueryResponseItem (instrument, source, time, ...) via
            string formatting, moreover the file-name of the file downloaded can
            be referred to as file, e.g.
            "{source}/{instrument}/{time.start}/{file}".

        methods : {list of str}
            Download methods, defaults to URL-FILE_Rice then URL-FILE.
            Methods are a concatenation of one PREFIX followed by any number of
            SUFFIXES i.e. `PREFIX-SUFFIX_SUFFIX2_SUFFIX3`.
            The full list of `PREFIXES <http://sdac.virtualsolar.org/cgi/show_details?keyword=METHOD_PREFIX>`_
            and `SUFFIXES <http://sdac.virtualsolar.org/cgi/show_details?keyword=METHOD_SUFFIX>`_
            are listed on the VSO site.

        downloader : sunpy.net.downloader.Downloader
            Downloader used to download the data.

        site : str
            There are a number of caching mirrors for SDO and other
            instruments, some available ones are listed below.

            =============== ========================================================
            NSO             National Solar Observatory, Tucson (US)
            SAO  (aka CFA)  Smithonian Astronomical Observatory, Harvard U. (US)
            SDAC (aka GSFC) Solar Data Analysis Center, NASA/GSFC (US)
            ROB             Royal Observatory of Belgium (Belgium)
            MPS             Max Planck Institute for Solar System Research (Germany)
            UCLan           University of Central Lancashire (UK)
            IAS             Institut Aeronautique et Spatial (France)
            KIS             Kiepenheuer-Institut fur Sonnenphysik Germany)
            NMSU            New Mexico State University (US)
            =============== ========================================================

        Returns
        -------
        out : :py:class:`Results` object that supplies a list of filenames with meta attributes containing the respective QueryResponse.

        Examples
        --------
        >>> res = get(qr).wait() # doctest:+SKIP
        """
        if downloader is None:
            downloader = download.Downloader()
            downloader.init()
            res = download.Results(
                lambda _: downloader.stop(), 1,
                lambda mp: self.link(query_response, mp)
            )
        else:
            res = download.Results(
                lambda _: None, 1, lambda mp: self.link(query_response, mp)
            )
        if path is None:
            path = os.path.join(config.get('downloads', 'download_dir'),
                                '{file}')
        path = os.path.expanduser(path)

        fileids = VSOClient.by_fileid(query_response)
        if not fileids:
            res.poke()
            return res
        # Adding the site parameter to the info
        info = {}
        if site is not None:
            info['site'] = site

        self.download_all(
            self.api.service.GetData(
                self.make_getdatarequest(query_response, methods, info)),
            methods, downloader, path,
            fileids, res
        )
        res.poke()
        return res

    @staticmethod
    def link(query_response, maps):
        """ Return list of paths with records associated with them in
        the meta attribute. """
        if not maps:
            return []
        ret = []

        for record_item in query_response:
            try:
                item = _Str(maps[record_item.fileid]['path'])
            except KeyError:
                continue
            # pylint: disable=W0201
            item.meta = record_item
            ret.append(item)
        return ret

    def make_getdatarequest(self, response, methods=None, info=None):
        """ Make datarequest with methods from response. """
        if methods is None:
            methods = self.method_order + ['URL']

        return self.create_getdatarequest(
            dict((k, [x.fileid for x in v])
                 for k, v in iteritems(self.by_provider(response))),
            methods, info
        )

    def create_getdatarequest(self, maps, methods, info=None):
        """ Create datarequest from maps mapping data provider to
        fileids and methods, """
        if info is None:
            info = {}

        return self.make(
            'VSOGetDataRequest',
            request__method__methodtype=methods,
            request__info=info,
            request__datacontainer__datarequestitem=[
                self.make('DataRequestItem', provider=k, fileiditem__fileid=[v])
                for k, v in iteritems(maps)
            ]
        )

    # pylint: disable=R0913,R0912
    def download_all(self, response, methods, dw, path, qr, res, info=None):
        GET_VERSION = [
            ('0.8', (5, 8)),
            ('0.7', (1, 4)),
            ('0.6', (0, 3)),
        ]
        for dresponse in response.getdataresponseitem:
            for version, (from_, to) in GET_VERSION:
                if getattr(dresponse, version, '0.6') >= version:
                    break
            else:
                res.add_error(UnknownVersion(dresponse))
                continue

            # If from_ and to are uninitialized, the else block of the loop
            # continues the outer loop and thus this code is never reached.
            # pylint: disable=W0631
            code = (
                dresponse.status[from_:to]
                if hasattr(dresponse, 'status') else '200'
            )
            if code == '200':
                for dataitem in dresponse.getdataitem.dataitem:
                    try:
                        self.download(
                            dresponse.method.methodtype[0],
                            dataitem.url,
                            dw,
                            res.require(
                                list(map(str, dataitem.fileiditem.fileid))),
                            res.add_error,
                            path,
                            qr[dataitem.fileiditem.fileid[0]]
                        )
                    except NoData:
                        res.add_error(DownloadFailed(dresponse))
                        continue
                    except Exception:
                        # FIXME: Is this a good idea?
                        res.add_error(DownloadFailed(dresponse))
            elif code == '300' or code == '412' or code == '405':
                if code == '300':
                    try:
                        methods = self.multiple_choices(
                            dresponse.method.methodtype, dresponse
                        )
                    except NoData:
                        res.add_error(MultipleChoices(dresponse))
                        continue
                elif code == '412':
                    try:
                        info = self.missing_information(
                            info, dresponse.info
                        )
                    except NoData:
                        res.add_error(MissingInformation(dresponse))
                        continue
                elif code == '405':
                    try:
                        methods = self.unknown_method(dresponse)
                    except NoData:
                        res.add_error(UnknownMethod(dresponse))
                        continue

                files = []
                for dataitem in dresponse.getdataitem.dataitem:
                    files.extend(dataitem.fileiditem.fileid)

                request = self.create_getdatarequest(
                    {dresponse.provider: files}, methods, info
                )

                self.download_all(
                    self.api.service.GetData(request), methods, dw, path,
                    qr, res, info
                )
            else:
                res.add_error(UnknownStatus(dresponse))

    def download(self, method, url, dw, callback, errback, *args):
        """ Override to costumize download action. """
        if method.startswith('URL'):
            return dw.download(url, partial(self.mk_filename, *args),
                        callback, errback
            )
        raise NoData

    @staticmethod
    def by_provider(response):
        """ 
        Returns a dictionary of provider 
        corresponding to records in the response. 
        """
        
        map_ = defaultdict(list)
        for record in response:
            map_[record.provider].append(record)
        return map_

    @staticmethod
    def by_fileid(response):
        """ 
        Returns a dictionary of fileids 
        corresponding to records in the response. 
        """
        
        return dict(
            (record.fileid, record) for record in response
        )

    # pylint: disable=W0613
    def multiple_choices(self, choices, response):
        """ Override to pick between multiple download choices. """
        for elem in self.method_order:
            if elem in choices:
                return [elem]
        raise NoData

    # pylint: disable=W0613
    def missing_information(self, info, field):
        """ Override to provide missing information. """
        raise NoData

    # pylint: disable=W0613
    def unknown_method(self, response):
        """ Override to pick a new method if the current one is unknown. """
        raise NoData

    @classmethod
    def _can_handle_query(cls, *query):
        return all([x.__class__.__name__ in attrs.__all__ for x in query])



class InteractiveVSOClient(VSOClient):
    """ Client for use in the REPL. Prompts user for data if required. """
    def multiple_choices(self, choices, response):
        """
        not documented yet

        Parameters
        ----------

            choices : not documented yet

            response : not documented yet

        """
        while True:
            for n, elem in enumerate(choices):
                print("({num:d}) {choice!s}".format(num=n + 1, choice=elem))
            try:
                choice = input("Method number: ")
            except KeyboardInterrupt:
                raise NoData
            if not choice:
                raise NoData
            try:
                choice = int(choice) - 1
            except ValueError:
                continue
            if choice == -1:
                raise NoData
            elif choice >= 0:
                try:
                    return [choices[choice]]
                except IndexError:
                    continue

    def missing_information(self, info, field):
        """
        not documented yet

        Parameters
        ----------
        info : not documented yet
                not documented yet
        field : not documented yet
            not documented yet

        Returns
        -------
        choice : not documented yet

        .. todo::
            improve documentation. what does this function do?

        """
        choice = input(field + ': ')
        if not choice:
            raise NoData
        return choice

    def search(self, *args, **kwargs):
        """ When passed an Attr object, perform new-style query;
        otherwise, perform legacy query.
        """
        if isinstance(args[0], Attr):
            return self.query(*args)
        else:
            return self.query_legacy(*args, **kwargs)

    def get(self, query_response, path=None, methods=('URL-FILE',), downloader=None):
        """The path expands ``~`` to refer to the user's home directory.
        If the given path is an already existing directory, ``{file}`` is
        appended to this path. After that, all received parameters (including
        the updated path) are passed to :meth:`VSOClient.get`.

        """
        if path is not None:
            path = os.path.abspath(os.path.expanduser(path))
            if os.path.exists(path) and os.path.isdir(path):
                path = os.path.join(path, '{file}')
        return VSOClient.get(self, query_response, path, methods, downloader)


g_client = None
def search(*args, **kwargs):
    # pylint: disable=W0603
    global g_client
    if g_client is None:
        g_client = InteractiveVSOClient()
    return g_client.search(*args, **kwargs)

search.__doc__ = InteractiveVSOClient.search.__doc__

def get(query_response, path=None, methods=('URL-FILE',), downloader=None):
    # pylint: disable=W0603
    global g_client
    if g_client is None:
        g_client = InteractiveVSOClient()
    return g_client.get(query_response, path, methods, downloader)

get.__doc__ = VSOClient.get.__doc__

if __name__ == "__main__":
    from sunpy.net import vso

    client = VSOClient()
    result = client.query(
        vso.attrs.Time((2011, 1, 1), (2011, 1, 1, 10)),
        vso.attrs.Instrument('aia')
    )
    #res = client.get(result, path="/download/path").wait()
