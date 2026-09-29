import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { UPLOAD_FORMAT_LABELS, importFormatLabel } from '../src/shared/config/labels.ts';

const openapi = JSON.parse(readFileSync(new URL('../../contracts/openapi.json', import.meta.url), 'utf8'));
const FORMATS = openapi.components.schemas.ImportFile.properties.format.enum;

test('import format: every contract format has a Russian label (QA 29.09, D6)', () => {
  for (const format of FORMATS) {
    const label = importFormatLabel({ format });
    assert.notEqual(label, format, `no label for ${format}`);
    assert.match(label, /[а-яё]/iu);
  }
});

test('import format: an API batch is told apart by its container, uploads list files only', () => {
  assert.equal(importFormatLabel({ format: 'journal_json', source_container: 'json' }), 'пачка API (JSON)');
  assert.equal(importFormatLabel({ format: 'journal_json', source_container: null }), 'пачка API (JSON)');
  assert.equal(importFormatLabel({ format: 'journal_json', source_container: 'xml' }), 'пачка API (XML)');
  assert.equal(importFormatLabel({ format: 'journal_csv', source_container: 'xlsx' }), 'журнал событий (CSV или XLSX)');
  assert.equal(importFormatLabel({ format: 'unknown_future' }), 'unknown_future');
  assert.ok(!('journal_json' in UPLOAD_FORMAT_LABELS) && !('journal_xml' in UPLOAD_FORMAT_LABELS));
});
