# Bundled certificates

`russian_trusted_root_ca.pem` is the public root certificate of the Russian Ministry of Digital Development (CN=Russian Trusted Root CA, valid until 2032-02-27,
SHA-256 D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31).
The MAX Bot API (platform-api2.max.ru) is signed by it. matele trusts it **only** inside the MAX client, on top of certifi's public roots;
it is never added to the system store.
