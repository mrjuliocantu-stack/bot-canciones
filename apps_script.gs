// Pega esto en tu Google Sheet: Extensiones → Apps Script.
// Cambia CLAVE por una palabra secreta (la misma que pondrás en Railway como SHEETS_KEY).
const CLAVE = 'cambia-esta-clave';
const HOJA = 'Leads';
const COLS = ['id','fecha','nombre','telefono','zona','servicio','superficie','m2',
              'estimado','comentarios','origen','estado','actualizado','por'];

function hoja_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(HOJA);
  if (!sh) {
    sh = ss.insertSheet(HOJA);
    sh.appendRow(COLS);
    sh.setFrozenRows(1);
    sh.getRange(1, 1, 1, COLS.length).setFontWeight('bold');
  }
  return sh;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  const d = JSON.parse(e.postData.contents);
  if (d.k !== CLAVE) return json_({ ok: false, error: 'clave' });
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const sh = hoja_();
    if (d.action === 'add') {
      const l = d.lead;
      sh.appendRow(COLS.map(c => (l[c] === undefined ? '' : l[c])));
    } else if (d.action === 'estado') {
      const n = sh.getLastRow() - 1;
      if (n > 0) {
        const ids = sh.getRange(2, 1, n, 1).getValues().flat().map(String);
        const i = ids.lastIndexOf(String(d.id));
        if (i >= 0) {
          sh.getRange(i + 2, COLS.indexOf('estado') + 1, 1, 3).setValues([[d.estado, d.actualizado, d.por]]);
        }
      }
    }
    return json_({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  if (!e || !e.parameter || e.parameter.k !== CLAVE) return json_({ ok: false, error: 'clave' });
  const v = hoja_().getDataRange().getValues();
  const h = v.shift();
  return json_(v.map(r => Object.fromEntries(h.map((k, i) => [k, r[i] instanceof Date ? r[i].toISOString() : r[i]]))));
}
