const sqlite3 = require('sqlite3').verbose();
const db = new sqlite3.Database('/home/ubuntu/caeron-gateway/gateway.db');

db.all("SELECT key, value FROM config", [], (err, rows) => {
    if (err) throw err;
    rows.forEach((row) => {
        console.log(row.key + ": " + row.value);
    });
    db.close();
});
