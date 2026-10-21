import sys,struct,json
RF="<dH"; RS=struct.calcsize(RF)
def recs(p):
    f=open(p,"rb"); f.readline(); out=[]
    while True:
        h=f.read(RS)
        if len(h)<RS: break
        t,n=struct.unpack(RF,h); d=f.read(n); out.append((t,d))
    return out
q=recs(sys.argv[1]); r=recs(sys.argv[2])
qend=q[-1][0]; r0=r[0][0]; shift=qend+60-r0
qpart=[d for t,d in q if d[6]==4][-1]
r=[(t,(d[:29]+qpart[29:]) if d[6]==4 else d) for t,d in r]      # same drivers in the same cars both sessions
if len(sys.argv)>4:
    with open(sys.argv[4],"wb") as f:
        f.write((json.dumps({"writer":"combine","format_version":1,"record_framing":"<dH","packet_format_expected":2025})+"\n").encode())
        for t,d in r: f.write(struct.pack(RF,t,len(d))); f.write(d)
with open(sys.argv[3],"wb") as f:
    f.write((json.dumps({"writer":"combine","format_version":1,"record_framing":"<dH","packet_format_expected":2025})+"\n").encode())
    for t,d in q:
        f.write(struct.pack(RF,t,len(d))); f.write(d)
        if d[6]==2 and t < qend-25:      # car telemetry beside each lap data: the field at speed
            tel=bytearray(d[:29]); tel[6]=6; body=bytearray(1352-29)
            for i in range(20): struct.pack_into("<H",body,i*60,200)
            tel+=body; f.write(struct.pack(RF,t,len(tel))); f.write(bytes(tel))
    for t,d in r:
        d=bytearray(d)
        if len(d)>=19: struct.pack_into("<f",d,15,struct.unpack_from("<f",d,15)[0]+shift)
        f.write(struct.pack(RF,t+shift,len(d))); f.write(bytes(d))
print("quali",len(q),"race",len(r),"shift",shift)
