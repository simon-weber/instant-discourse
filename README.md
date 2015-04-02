# Instant Discourse

Instant Discourse aims to provide intelligent stranger chat.
Unlike alternatives like Omegle, Instant Discourse:

  * decreases noise by penalizing users who say anything non-unique (ala [#xkcd-signal](http://blog.xkcd.com/2008/01/14/robot9000-and-xkcd-signal-attacking-noise-in-chat/))
  * incentivizes longer conversations by enforcing delays between conversations (not actually implemented right now)
  * enforces privacy by keeping no logs and sending all messages peer-to-peer

There's a (half-baked) version live at https://instantdiscourse.com.

## Technical details:

### inside-out overview

- redis
  - message hashes
  - message hash bloom filter
- tornado
  - http
    - /peerjs/id: allocates unique peerjs ids
  - websocket
    - peerjs signaling for webrtc
    - chat signaling: getting a partner, reporting/checking message hashes, getting currently connected clients
- nginx
  - static files (ops/...html.j2)
  - tls termination
  - proxies to tornado
- cloudflare
  - instantdiscourse.com: https static files. served by cloudflare
  - app.instantdiscourse.com: websocket connections. cloudflare passthrough since they don't currently support websockets
- browser
  - peerjs to simplify the webrtc connection
  - a bunch of horrendous javascript for ui things


### duplicate message detection

To detect non-unique messages without keeping logs, clients report message hashes to the server.
Message hashes have no metadata associated with them;
they can only answer "has message X ever been sent", not "who sent message X" or "when was message X sent".

Hashes are peer-enforced to help prevent misreporting of fake hashes.
Here's an example conversation:

```
alice                           server                          bob
   -- who's my partner? -------->    <------ who's my partner? --
   <------ bob is your partner --    -- alice is your partner -->


   -- yo! ------------------------------------------------------>

   -- I said hash(yo!) ---------> 

                                     <---- they said hash(yo!) --

                          | check hash(yo!) |
                    | hash(yo!) unique, record it |


   <----------------------------------------------------- yo! --

                                     <------ I said hash(yo!) --

   -- they said hash(yo!) -----> 

                          | check hash(yo!) |
   <------------- hash(yo!) non-unique! penalize bob ---------->
```


If any unmatched hashes exist at the end of a conversation, it's likely that one partner was lying.
The server can't tell which one, but could record both and look for users who are often in unmatched conversations.
This prevents casual cheating between an honest and malicious partner.

Despite this, the hash database could still be filled up by a pair of malicious clients working together.
Please don't do that =)
