"""

Filename: generic_rain_alerter.py
Version: 0.2

Purpose: Downloads subsetted HRRR data
for a city-level domain, specified in a rain_alerter.ini
configuration file. Generates hourly precipitation accumulation
maps, and a 36 hour total map, then emails these maps to specified
recipients.

Author: Brandon Taylor
Date: 2019-08-03
Last Modified: 2019-09-08

  ****************COPYRIGHT STATEMENT***********************
  This software distributed WITHOUT ANY WARRANTY under the
  terms of the GNU Lesser General Public License Version 3.
  See the GNU Lesser General Public License for more details.
  ****************COPYRIGHT STATEMENT***********************

"""

import os
import sys
import io
import json
import copy
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
import smtplib
import six

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import cartopy.crs as ccrs
import cartopy.io.img_tiles as cimgt
from PIL import Image
import pygrib
from flask import Flask, render_template
from flask_mail import Mail, Message

MM_TO_IN = 0.0393701
HERE = '/home/brandontaylor42/rain_alerter/'
AREA = sys.argv[1]


with open(os.path.join(HERE, 'rain_alerter.json'), 'r') as jsfile:
    CONFIG = json.load(jsfile)

AREA_CONFIG = CONFIG['Areas'][AREA]
RECIPIENTS = AREA_CONFIG['Recipients']

NOMADS_URL_BASE = CONFIG['General']['NOMADSUrlBase']
UTC_OFFSET = timedelta(hours=CONFIG['General']['UTCOffset'])

L_LON = AREA_CONFIG['LeftLon']
R_LON = AREA_CONFIG['RightLon']
T_LAT = AREA_CONFIG['TopLat']
B_LAT = AREA_CONFIG['BottomLat']

ZOOM_LEVEL = AREA_CONFIG['ZoomLevel']

POINTS = AREA_CONFIG.get('Points')
LABEL_OFFSET = AREA_CONFIG['LabelOffset']

NOW = datetime.now(timezone.utc)
NOW_STR = NOW.strftime('%Y%m%d')

# Enlargen bounding box to ensure map is filled with data
LL = str(L_LON - 0.05)
RL = str(R_LON + 0.05)
TL = str(T_LAT + 0.05)
BL = str(B_LAT - 0.05)
REQUEST_PARAMS = '&var_APCP=on&subregion=&leftlon=' + LL \
             + '&rightlon=' + RL + '&toplat=' + TL \
             + '&bottomlat=' + BL + '&dir=%2Fhrrr.' \
             + NOW_STR + '%2Fconus'

INIT_HOUR = 12 if NOW.hour > 12 else 0
INIT_DATETIME = NOW.replace(hour=INIT_HOUR, minute=0)
GRIB_FILTER = 'filter_hrrr_2d.pl?file=hrrr.t' + '%02d' % INIT_HOUR + 'z.wrfsfcf'

def nearest_gridpoint(lats, lons, point_lat, point_lon, data):
    """
    Finds the nearest-neighbor in the gridded model data to any
    arbitrary lat/lon point.
    @param {np.ndarray} lats - 2-D numpy array of grid latitude
    @param {np.ndarray} lons - 2-D numpy array of grid longitude
    @param {float} point_lat - selected point latitude
    @param {float} point_lon - selected point longitude
    @param {np.ndarray} data - contains the model fields accumulated precipitation.
                          The fields are 2-D numpy array of same
                          shape as gridded lats/lons.
    @return {float} - contains the nearest gridpoint precip.
    """
    abslat = np.abs(lats - point_lat)
    abslon = np.abs(lons - point_lon)
    max_latlon = np.maximum(abslon, abslat)
    latlon_idx = np.argmin(max_latlon)
    gridpoint_precip = round(data.flat[latlon_idx], 2)
    return gridpoint_precip

def new_get_image(self, tile):
    """
    monkey patch for get_image method.
    Sets User-agent to ensure request isn't blocked.
    @param {class} self - GoogleWTS class
    @param {str} tile - image tile URL
    return {Image, tile bounding box, tile location}
    """
    url = self._image_url(tile)
    image_request = Request(url)
    image_request.add_header('User-agent', 'cartopybot/0.7')
    request_data = urlopen(image_request)
    image_data = six.BytesIO(request_data.read())
    request_data.close()
    image = Image.open(image_data)
    image_converted = image.convert(self.desired_tile_form)
    return image_converted, self.tileextent(tile), 'lower'

# override default method with monkey patch
cimgt.GoogleWTS.get_image = new_get_image

def retrieve_hrrr_data(hour, total=False):
    """
    Downloads grib data from NOMADS,
    as specified by the URL parameters.
    """
    hour_str = '%02d' % hour
    full_url = NOMADS_URL_BASE + GRIB_FILTER \
               + hour_str + '.grib2' + REQUEST_PARAMS
    request = Request(full_url)
    request.add_header('User-agent', 'routewx/0.2')
    grib_request = urlopen(request)
    bytestr = grib_request.read()
    if not total:
        bytestr_split = bytestr.split(b'7777')
        if len(bytestr_split) > 2:
            bytestr = bytestr_split[1] + b'7777'
    grb = pygrib.fromstring(bytestr)
    return grb.data()

def generate_hour_datestr(hour):
    """
    Generates a human-friendly string
    for the specified model prognostication hour
    """
    datetime_local = INIT_DATETIME + UTC_OFFSET + timedelta(hours=hour)
    hour_datestr = datetime_local.strftime('%m/%d/%Y %I:%M %p')
    return hour_datestr

class RainAlerter:
    """
    Retrieves and plots data for
    city specified in the command line
    arguement
    """
    tiler = cimgt.OSM()
    proj = tiler.crs
    trans = ccrs.PlateCarree()
    logo = mpimg.imread(os.path.join(HERE, 'routewx_logo.png'))
    def __init__(self, init_datestr):
        self.init_datestr = init_datestr
        self.total_img = None
        self.data_maximums = []
        self.hourly_data = dict((recipient, dict((point_label, []) for point_label in point)) 
                                for recipient, point in RECIPIENTS.items())
    def start(self):
        """
        Starts the loop over each hour
        """
        fig = plt.figure(figsize=(10, 10))
        for hour in range(1, 37):
            final_hour = hour == 36
            hrrr_data_tuple = retrieve_hrrr_data(hour, total=final_hour)
            hrrr_data_inches, max_precip_inches = self._data_conversion_and_max(hour, hrrr_data_tuple)
            hour_datestr = generate_hour_datestr(hour)
            if max_precip_inches > 0.05 and hour < 36:
                precip_inches, precip_lats, precip_lons = hrrr_data_inches
                for recipient in self.hourly_data:
                    for point in self.hourly_data[recipient]:
                        point_lat = POINTS[point]['Lat']
                        point_lon = POINTS[point]['Lon']
                        point_precip = nearest_gridpoint(precip_lats, precip_lons,
                                                         point_lat, point_lon,
                                                         precip_inches)
                        self.hourly_data[recipient][point].append({hour_datestr:point_precip})
            if final_hour:
                print(max_precip_inches)
                self.plot_total(hrrr_data_inches, hour_datestr, max_precip_inches)
    def _data_conversion_and_max(self, hour, hrrr_data):
        precip_mm, precip_lats, precip_lons = hrrr_data
        precip_inches = precip_mm * MM_TO_IN
        max_precip_inches = np.max(precip_inches)
        if hour < 36:
            self.data_maximums.append(max_precip_inches)
        hrrr_data_inches = (precip_inches, precip_lats, precip_lons)
        return (hrrr_data_inches, max_precip_inches)
    def _savefig_hourly(self, fig, precip_contour, plot_count):
        title_str = CITY_NAME + '\n1 Hr. Precip. (in.)'
        fig.suptitle(title_str)
        fig.tight_layout(rect=(0.00, 0.05, 0.95, 0.95))
        fig.subplots_adjust(right=0.85)
        cbar_ax = fig.add_axes([0.8, 0.09, 0.025, 0.79])
        fig.colorbar(precip_contour, cax=cbar_ax)
        logo_ax = fig.add_axes([0, 0, 0.2, 0.15])
        logo_ax.axis('off')
        logo_ax.imshow(self.logo)
        plot_fname = 'accum_thumb_' + str(plot_count / 4) + '.png'
        buf = io.BytesIO()
        fig.savefig(buf, dpi=300)
        buf.seek(0)
        img = MIMEImage(buf.read())
        img.add_header('Content-Disposition', "attachment; filename= %s" % plot_fname)
        self.msg.attach(img)
        buf.close()
        fig.clf()
    def plot_hourly(self, fig, hrrr_data, hour_datestr, plot_count):
        """
        Plots hourly accumlated precipitation data
        @param {object} fig - hourly matplotlib.figure.Figure instance.
        @param {tuple} hrrr_data - tuple containing gridded model data,
        and corresponding lats, lons.
        @param {str} hour_datestr - human-friendly string representing
        the current hour.
        @param {int} plot_count - Current number of plots generated.
        @return {object, QuadContourSet} fig, precip_contour
        """
        precip_inches, precip_lats, precip_lons = hrrr_data
        print(plot_count, 'plot_count')
        axes_idx = (plot_count + 1) % 4 if (plot_count + 1) % 4 > 0 else 1
        print(axes_idx, 'axes_idx')
        current_ax = fig.add_subplot(2, 2, axes_idx, projection=self.proj)
        precip_contour = current_ax.contourf(precip_lons, precip_lats,
                                             precip_inches,
                                             transform=self.trans,
                                             cmap=plt.cm.get_cmap('ocean_r'),
                                             vmin=0)
        current_ax.set_title(hour_datestr)
        current_ax.set_xticks([], [])
        current_ax.set_yticks([], [])
        return fig, precip_contour
    def plot_total(self, hrrr_data, hour_datestr, max_precip_inches):
        """
        Plots total accumulated precipitation
        """
        precip_inches, precip_lats, precip_lons = hrrr_data
        fig = plt.figure(figsize=(10, 10))
        total_ax = plt.axes(projection=self.proj)
        total_ax.set_extent([L_LON, R_LON, B_LAT, T_LAT], crs=self.trans)
        total_ax.add_image(self.tiler, ZOOM_LEVEL)
        precip_contour = total_ax.contourf(precip_lons, precip_lats, precip_inches,
                                           transform=self.trans,
                                           cmap=plt.get_cmap('ocean_r'), alpha=0.5)
        if False:
            for label, coords in POINTS.items():
                point_lon = coords['Lon']
                point_lat = coords['Lat']
                total_ax.scatter(point_lon, point_lat, color='k', linewidth=2, marker='o',
                             transform=self.trans)
                total_ax.text(point_lon - LABEL_OFFSET,
                              point_lat - LABEL_OFFSET,
                              label, transform=self.trans)
                grid_precip = nearest_gridpoint(precip_lats, precip_lons,
                                                point_lat, point_lon,
                                                precip_inches)
                total_ax.text(point_lon + LABEL_OFFSET, point_lat + LABEL_OFFSET,
                              str(grid_precip) + ' inches', transform=self.trans)
        fig.colorbar(precip_contour, ax=total_ax)
        plt.title(AREA + '\nTotal Precip (in.) - ' +
                  self.init_datestr + ' to ' + hour_datestr)
        logo_ax = fig.add_axes([0, 0, 0.2, 0.15])
        logo_ax.axis('off')
        logo_ax.imshow(self.logo)
        if max_precip_inches > 0:
            self._savefig_total(fig)
    def _savefig_total(self, fig):
        buf = io.BytesIO()
        fig.savefig(buf, dpi=300)
        buf.seek(0)
        img = buf.read()
        self.total_img = img
        buf.close()

def compose_message(app, mail):
    """
    Compose the alert email
    """

    init_datestr = generate_hour_datestr(0)
    alerter = RainAlerter(init_datestr)
    alerter.start()

    hours_light = len([m for m in alerter.data_maximums if 0.05 <= m < 0.25])
    hours_mod = len([m for m in alerter.data_maximums if 0.25 <= m <= 0.5])
    hours_heavy = len([m for m in alerter.data_maximums if m > 0.5])
    total = round(max(alerter.data_maximums), 2)
    send = True

    if total < 0.05:
        subject = 'No measurable rain in the next 36 hours'
        send = False
    elif total > 0.5:
        subject = 'Heavy rain expected in the next 36 hours'
    elif hours_heavy > 0:
        subject = 'Some heavy rain expected in the next 36 hours'
    elif hours_mod > 0:
        subject = 'Some moderate rain expected in the next 36 hours'
    elif hours_light > 0:
        subject = 'Some light rain expected in the next 36 hours'
    else:
        subject = 'Some rain expected in the next 36 hours'

    msg_subject = AREA + ' Area: ' + subject
    end_datestr = generate_hour_datestr(36)
    msg_text = 'From ' + init_datestr + ' to ' + end_datestr + ':\n' + \
               str(hours_light) + ' hours of light precipitation, \n' + str(hours_mod) + \
               ' hours of moderate precip, and\n' + str(hours_heavy) + \
               ' hours of heavy precip can be expected. \n' + \
               'A max total of ' + str(total) + \
               ' inches of precip can be expected over the next 36 hours.'
    if send:
        with app.app_context():
            with mail.connect() as conn:
                for recipient in RECIPIENTS:
                    msg = Message(recipients=[recipient], body="", 
                                  subject=msg_subject)
                    total_fname = init_datestr + '_thru_' + end_datestr + '_total_accum.png'
                    print('Attaching total_accum plot to email: ' + total_fname)
                    msg.attach(total_fname, "image/png", alerter.total_img)
                    msg.html = render_template('alerter_email.html',
                                               msg_text=msg_text,
                                               hourly_data=alerter.hourly_data[recipient],
                                               recipient=recipient)
                    conn.send(msg)

if __name__ == '__main__':
    app = Flask(__name__)
    app.config.from_envvar('RAIN_ALERTER_CONFIG')
    mail = Mail(app)
    compose_message(app, mail)
