#!/bin/bash

source /home/brandontaylor42/miniconda3/etc/profile.d/conda.sh
export MPLBACKEND="Agg"
export RAIN_ALERTER_CONFIG=/home/brandontaylor42/rain_alerter/mail.cfg
conda activate rain-alerter
python /home/brandontaylor42/rain_alerter/rain-alerter/generic_rain_alerter.py Bartlesville
python /home/brandontaylor42/rain_alerter/rain-alerter/generic_rain_alerter.py Norman
python /home/brandontaylor42/rain_alerter/rain-alerter/generic_rain_alerter.py JBU
