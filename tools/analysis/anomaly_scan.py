import csv,sys,statistics
from collections import Counter
f=sys.argv[1]
r=list(csv.DictReader(open(f)))
tcol='elapsed_sec'
for x in r:
    x['t']=float(x[tcol]); x['rpm_']=float(x['rpm']); x['tps']=float(x['tps_pct']); x['adv']=float(x['timing_advance_deg'])
n=len(r)
# classify dropouts: tps closed (<=4) but neighbours (within 2 samples each side) clearly open
def openish(i):
    return r[i]['tps']>12
drop=[]
for i in range(2,n-2):
    if r[i]['tps']<=4.0:
        before=any(openish(j) for j in (i-1,i-2))
        after=any(openish(j) for j in (i+1,i+2))
        if before and after:
            drop.append(i)
print('== ISOLATED CLOSED-THROTTLE DROPOUTS (open throttle both sides) ==')
print('count',len(drop),'of',n,'samples over %.0f s'%r[-1]['t'])
print('idx      t     rpm  tps_prev tps tps_next  adv  rpm_next d_rpm%')
for i in drop:
    dr=(r[i+1]['rpm_']-r[i-1]['rpm_'])/max(1,r[i-1]['rpm_'])*100
    print('%4d %7.2f %6.0f  %6.2f %5.2f %6.2f  %5.1f  %6.0f %+6.1f'%(i,r[i]['t'],r[i]['rpm_'],r[i-1]['tps'],r[i]['tps'],r[i+1]['tps'],r[i]['adv'],r[i+1]['rpm_'],dr))
print()
print('rpm at dropout: ', ['%.0f'%r[i]['rpm_'] for i in drop])
print('tps before dropout:', ['%.0f'%r[i-1]['tps'] for i in drop])
print('time buckets (60s):',Counter(int(r[i]['t']//60)*60 for i in drop))
print('coolant at dropout:',Counter(r[i]['coolant_temp_c'] for i in drop))
