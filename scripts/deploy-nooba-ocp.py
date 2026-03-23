import schedule
import datetime
import time
import os

def get_suffix():
    nowtime = datetime.datetime.now()
    month = nowtime.strftime("%b").lower()
    day = str(nowtime.day)
    return month + '-' + day + '-' + '24'

def job(t):
    try:
        print("executing")
        suffix = get_suffix()
        os.system(f"deploy-ocp --webhook-url 'https://chat.googleapis.com/v1/spaces/_qx5zQAAAAE/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=3Ey5VmCO9V3i0WPaF064UuGWYoiwY5b8P7wAUe26Ggc' --ocp4mcoci-conf samples/deploy_nooba_cluster/override_config.yaml --cluster-name nooba-{suffix} --cluster-path /tmp/nooba-{suffix}")
    except Exception:
        pass
# 09:00
for i in ["10:30"]:
    schedule.every().monday.at(i).do(job, i)
    schedule.every().tuesday.at(i).do(job, i)
    schedule.every().wednesday.at(i).do(job, i)
    schedule.every().thursday.at(i).do(job, i)
    schedule.every().friday.at(i).do(job, i)

while True:
    schedule.run_pending()
    time.sleep(1)
