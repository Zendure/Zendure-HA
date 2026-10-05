# EMQX 6 setup for Zendure devices

After upgrading the EMQX add-on to version 6, Zendure devices stop reporting. They still
have power, they are still on your wifi, and their own web page still answers, but nothing
arrives in Home Assistant any more. This is not a broken device and not a broken
integration. EMQX 6 changed three defaults, and each one on its own is enough to lock the
devices out.

Work through the three steps below in order. You need the EMQX dashboard (port 18083) and,
for step 3, one command in a terminal on the Home Assistant machine.

## Step 1: allow publishing and subscribing again

EMQX 6 changed `authorization.no_match` from `allow` to `deny`. That setting decides what
happens when no authorization rule matches a publish or a subscribe. Until version 5 the
answer was "allow it", from version 6 it is "refuse it". If your broker has no rules of its
own, everything is refused, including Home Assistant's own MQTT entities.

In the dashboard: **Access Control** > **Authorization** > tab **Settings** > field
**No Match Action** > set to `allow` > save.

Prefer to keep `deny`? Then add explicit rules under **Access Control** > **Authorization**
> **Built-in Database**: permission `allow`, action `all`, topic `#`, for every user that
connects. Forget one and that client goes silent.

Symptom if this step is missing: a client can connect, but a publish comes back as
`Not authorized` and every subscribe is refused.

## Step 2: widen the cipher list on the TLS listener

The devices connect over TLS on port 8883, and they offer older cipher suites than EMQX 6
accepts by default. The handshake then fails before any MQTT connection exists, which is why
the broker shows no error counter at all.

In the dashboard: **Management** > **Listeners** > click the SSL listener **default** (that
is port 8883) > **Edit Listener**.

- **SSL Versions**: `tlsv1.2` and `tlsv1.3` both enabled.
- **Cipher Suites**: the list below. Keep the modern ones, or you lock out the clients that
  do work today.

```
TLS_AES_256_GCM_SHA384,TLS_AES_128_GCM_SHA256,ECDHE-RSA-AES256-GCM-SHA384,ECDHE-RSA-AES128-GCM-SHA256,ECDHE-RSA-AES128-SHA,AES256-SHA,AES128-SHA
```

Symptom if this step is missing: the broker log says `no shared cipher`, and the device
retries every few seconds without ever connecting.

## Step 3: present the certificate the devices trust

This is the one that catches most people. Zendure devices carry a fixed list of trusted
certificate authorities in their firmware, and EMQX's old demo CA is in that list:

```
CN=RootCA, O=EMQ, ST=hangzhou, C=CN     serial f3d80a34e6c5db7f, valid until 2030-05-06
```

EMQX 6 ships a completely new demo certificate, issued by a different root:

```
CN=localhost, O=EMQ, ST=Stockholm, C=SE   issued by CN=RootCA, O=EMQ, ST=Stockholm, C=SE
```

The devices do not know that new root, so they abort the handshake themselves with the TLS
alert `unknown_ca` and reset the connection. Nothing reaches the MQTT layer.

The fix is to give the listener the old certificate again. It is a public test certificate
from the EMQX repository. In a terminal on the Home Assistant machine:

```bash
sudo wget -O /ssl/server-cert.pem https://raw.githubusercontent.com/emqx/emqx/v5.0.0/apps/emqx/etc/certs/cert.pem
```

```bash
sudo wget -O /ssl/server-key.pem https://raw.githubusercontent.com/emqx/emqx/v5.0.0/apps/emqx/etc/certs/key.pem
```

Then in the dashboard, on the same **ssl:default** listener:

- **TLS Cert**: `/ssl/server-cert.pem`
- **TLS Key**: `/ssl/server-key.pem`
- **Verify Peer**: off

There is no CA certificate to fill in. With Verify Peer off the broker does not check
anything the device sends, and the certificate above is signed directly by the root the
device already trusts, so there is no intermediate to pass along.

The certificate and key are also kept in this repository under `ssl/`, together with the
matching CA certificate, so you do not have to hunt for them.

## Step 4: let everything reconnect

Once a client has been refused it does not always try again on its own.

- Reload the **MQTT** integration and the **EnergyMaster** integration in Home Assistant.
  A refused subscription is not retried by itself, so without a reload they stay deaf.
- Devices that gave up during the broken period may need a power cycle. Devices that are
  still retrying come back within a minute.

## Checking that it worked

In the EnergyMaster device list the connection of each device should read `LOCAL`. Two other
quick checks:

- The dashboard under **Monitoring** > **Clients** shows one entry per device.
- On the Home Assistant machine, `netstat -tan | grep 8883` shows an `ESTABLISHED` line per
  device. A line that keeps reappearing with a new port number means the handshake is
  failing, not succeeding.

## Symptom table

| What you see | Cause | Step |
|---|---|---|
| Publish comes back as `Not authorized`, subscribe refused | `no_match` is `deny` | 1 |
| Broker log says `no shared cipher` | cipher list too strict | 2 |
| Device sends TLS alert `unknown_ca` and resets | wrong certificate | 3 |
| Entities show `unavailable`, broker sees no connection at all | device gave up | 4 |
| Device answers on its own web page but nothing in Home Assistant | any of the above | 1, 2, 3 |

## A word about security

The certificate in step 3 is a demo certificate, and its private key is published in EMQX's
own repository. Anyone on your local network could therefore pose as your broker. On a home
network with devices that accept nothing else this is a reasonable trade, but do not expose
port 8883 to the internet with this certificate in place.

## Ports at a glance

| Port | Use |
|---|---|
| 1883 | MQTT without TLS |
| 8883 | MQTT over TLS, this is what the devices use |
| 18083 | the EMQX dashboard |
