function InstantDiscourse(tornado_host, tornado_port){
    var connectedPeer;

    var me_in_timeout = false;
    var my_timer = null;

    var partner_in_timeout = false;
    var partner_timer = null;

    function connect(c) {
        console.log(c.peer, 'partner connected to me');
        connectedPeer = c.peer;
        var chatbox = $('#chatlog');
        /* This div id is used by tests to tell when a connection happens. */
        chatbox.append('<div id="connect-' + connectedPeer + '"><em>Peer connected.</em></div>');

        c.on('data', function(data) {
            data = JSON.parse(data);
            console.log('from other peer:', data);
            if (data.type == 'chat'){
                if (partner_in_timeout){
                    console.log('partner sent a message during timeout:', data.message);
                }
                else{
                    console.log('adding message to', chatbox);
                    /* Div class used in tests. */
                    var msg = $('<div class="message from-peer">Peer: ' + data.message + '</div>');
                    chatbox.append(msg);
                    msg[0].scrollIntoView();
                }
                peer.socket.send({
                    type: 'INSTANT_DISCOURSE',
                    subtype: 'them-hash',
                    hash: data.message,
                });
            }
            else if (data.type == 'end-timeout'){
                end_my_timeout();
            }
            else{
                console.log('unrecognized peer message', data);
            }

        });
        c.on('close', function() {
            chatbox.append('<div id="disconnect-' + connectedPeer + '"><em>Peer disconnected.</em></div>');
            connectedPeer = null;
            $('#get-partner').show();
            $('#close').hide();
        });
    }

    function ChatPeer(options){
        Peer.call(this, options);
    }
    ChatPeer.prototype = Object.create(Peer.prototype);
    ChatPeer.prototype._handleMessage = function(message){
        console.log('chatpeer handlemessage', message);
        if(message.type == 'INSTANT_DISCOURSE'){
            console.log('app-specific message:', message); 

            if (message.hasOwnProperty('num_clients')) {
                $('#num-clients').text(message.num_clients);
            }

            if (message.subtype == 'match'){
                console.log('got partner!', message.match_cid);
                if (!connectedPeer){
                    connectedPeer = message.match_cid;
                    var c = peer.connect(message.match_cid, {
                        label: 'chat',
                        serialization: 'none',
                        reliable: false,
                        metadata: {message: 'hi i want to chat with you!'}
                    });
                    c.on('open', function() {
                        connect(c);
                    });
                    c.on('error', function(err) { alert(err); });
                }
            }
            else if (message.subtype == 'you-penalty') {
                console.log('i got a penalty!');
                add_to_my_timeout(message.duration_secs * 1000);
            }
            else if (message.subtype == 'them-penalty') {
                console.log('partner got a penalty!');
                add_to_partner_timeout(message.duration_secs * 1000);
            }
            else {
                console.log('unhandled ID message subtype', message);
            }

        } 
        else {
            console.log('peerjs message', message);
            Peer.prototype._handleMessage.call(this, message);
        }
    };

    var peer = new ChatPeer({
        host: tornado_host,
        port: tornado_port,
        path: '/',

        // choose 0 to 3 inclusive
        debug: 2,

        // Use a TURN server for more network support
        config: {'iceServers': [
            { url: 'stun:stun.l.google.com:19302' }
            ]} /* Sample servers, please use appropriate ones */
    });

    peer.on('open', function(id){
        $('#pid').text(id);
        peer.socket.send({
            type: 'INSTANT_DISCOURSE',
            subtype: 'get-num-clients'
        });
    });

    function get_peer_connection(){
        var conns = peer.connections[connectedPeer];
        console.assert(conns.length == 1);
        return conns[0];
    }

    function add_to_my_timeout(duration_ms){
        me_in_timeout = true;
        $("#text").attr("disabled", "disabled");

        /* the other peer enforces this now.
        console.log('adding to timeout:', duration_ms);
        if (!my_timer){
            console.log('creating my_timer');
            my_timer = new MyTimer(end_my_timeout, duration_ms);
        }
        else {
            console.log('add to existing my_timer');
            my_timer.add(duration_ms);
        }
        */
    }

    function end_my_timeout(){
        console.log('my timeout ended');
        $("#text").removeAttr("disabled");
        me_in_timeout = false;
    }

    function add_to_partner_timeout(duration_ms){
        partner_in_timeout = true;
        $("#partnertimeout").text(' yes');
        console.log('adding to partner timeout:', duration_ms);
        if (!partner_timer){
            console.log('creating partner_timer');
            partner_timer = new PartnerTimer(end_partner_timeout, duration_ms);
        }
        else {
            console.log('add to existing partner_timer');
            partner_timer.add(duration_ms);
        }
    }

    function end_partner_timeout(){
        console.log('partner timeout ended');
        $("#partnertimeout").text(" no");
        partner_in_timeout = false;
        get_peer_connection().send(JSON.stringify({type: 'end-timeout'}));
    }

    // Await connections from others
    peer.on('connection', connect);

    $(document).ready(function() {
        // Close a connection.
        $('#close').click(function() {
            get_peer_connection().close();
            $('#get-partner').show();
            $('#close').hide();
        });

        $('#get-partner').click(function() {
            console.log('get-partner');
            peer.socket.send({
                type: 'INSTANT_DISCOURSE',
                subtype: 'get-partner'
            });
            $('#get-partner').hide();
            $('#close').show();
        });

        $('#send').submit(function(e) {
            e.preventDefault();
            var msg = $('#text').val();
            get_peer_connection().send(JSON.stringify({type: 'chat', message: msg}));
            $('#chatlog').append('<div class="message from-me">You: ' + msg + '</div>');
            $('#text').val('');
            $('#text').focus();
            peer.socket.send({
                type: 'INSTANT_DISCOURSE',
                subtype: 'me-hash',
                hash: msg,
            });
        });

        // Show browser version
        $('#browsers').text(navigator.userAgent);
    });

    // Make sure things clean up properly.

    window.onunload = window.onbeforeunload = function(e) {
        if (!!peer && !peer.destroyed) {
            peer.destroy();
        }
    };

    /* source: http://stackoverflow.com/a/7798773/1231454 */
    function MyTimer(callback, ms) {
        this.setTimeout(callback, ms);
    }

    MyTimer.prototype.setTimeout = function(callback, ms) {
        var self = this;
        if(this.timer) {
            clearTimeout(this.timer);
        }
        this.finished = false;
        this.callback = callback;
        this.ms = ms;
        this.timer = setTimeout(function() {
            self.finished = true;
            callback();
        }, ms);
        this.start = Date.now();
    };

    MyTimer.prototype.add = function(ms) {
        if(!this.finished) {
            // add time to time left
            ms = this.ms - (Date.now() - this.start) + ms;
            this.setTimeout(this.callback, ms);
        }
        else {
            my_timer = null;
            add_to_my_timeout(ms);
        }
    };

    /* hahahaha */
    function PartnerTimer(callback, ms) {
        this.setTimeout(callback, ms);
    }

    PartnerTimer.prototype.setTimeout = function(callback, ms) {
        var self = this;
        if(this.timer) {
            clearTimeout(this.timer);
        }
        this.finished = false;
        this.callback = callback;
        this.ms = ms;
        this.timer = setTimeout(function() {
            self.finished = true;
            callback();
        }, ms);
        this.start = Date.now();
    };

    PartnerTimer.prototype.add = function(ms) {
        if(!this.finished) {
            // add time to time left
            ms = this.ms - (Date.now() - this.start) + ms;
            this.setTimeout(this.callback, ms);
        }
        else {
            partner_timer = null;
            add_to_partner_timeout(ms);
        }
    };
}
