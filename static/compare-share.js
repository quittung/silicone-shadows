/* Versioned, self-contained comparison links. Keep v1 decoding stable. */
(() => {
  const MAX_URL_LENGTH = 7500;
  const MAX_ENCODED_LENGTH = 7400;
  const MAX_BYTES = 256000;
  const MAX_OBJECTS = 2000;
  const invalid = () => new Error('This comparison link is invalid or too large.');
  const tooLarge = () => new Error('This arrangement is too large for a share link. Remove some outlines and try again.');

  function validate(data) {
    if (!Array.isArray(data) || data.length !== 2 || ![0, 1].includes(data[0]) ||
        !Array.isArray(data[1]) || !data[1].length || data[1].length > MAX_OBJECTS) throw invalid();
    for (const row of data[1]) {
      if (!Array.isArray(row) || row.length !== 7) throw invalid();
      const [id, size, x, y, angle, mirrored, color] = row;
      if (!(typeof id === 'string' && id.length > 0 && id.length <= 200 ||
            Number.isSafeInteger(id) && id >= 0) ||
          !Number.isSafeInteger(size) || size < 0 || size > 10000 ||
          ![x, y].every(value => Number.isSafeInteger(value) && Math.abs(value) <= 10000000) ||
          !Number.isInteger(angle) || angle < -1800 || angle >= 1800 ||
          ![0, 1].includes(mirrored) || !Number.isInteger(color) || color < 0 || color > 7) throw invalid();
    }
    return data;
  }

  async function readBounded(stream) {
    const reader = stream.getReader();
    const chunks = [];
    let length = 0;
    try {
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        length += value.length;
        if (length > MAX_BYTES) throw invalid();
        chunks.push(value);
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    return bytes;
  }

  // v1: zlib-wrapped DEFLATE + unpadded base64url of UTF-8 JSON [units, rows].
  // Rows: [stable product ID, size index, x/0.01in, y/0.01in,
  //        rotation from default/0.1deg, mirrored, palette index]. Order is stacking.
  async function encode(data) {
    if (typeof CompressionStream !== 'function') throw new Error('Update your browser to create comparison links.');
    validate(data);
    const json = new TextEncoder().encode(JSON.stringify(data));
    if (json.length > MAX_BYTES) throw tooLarge();
    const bytes = await readBounded(new Blob([json]).stream().pipeThrough(new CompressionStream('deflate')));
    if (bytes.length > MAX_ENCODED_LENGTH * 3 / 4) throw tooLarge();
    const encoded = btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
    if (encoded.length > MAX_ENCODED_LENGTH) throw tooLarge();
    return `1.${encoded}`;
  }

  async function decode(value) {
    if (typeof DecompressionStream !== 'function') throw new Error('Update your browser to open this comparison link.');
    if (!value.startsWith('1.')) throw new Error('This comparison link uses an unsupported format.');
    const encoded = value.slice(2);
    if (!encoded || encoded.length > MAX_ENCODED_LENGTH || !/^[\w-]+$/.test(encoded)) throw invalid();
    try {
      const bytes = Uint8Array.from(atob(encoded.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - encoded.length % 4) % 4)), c => c.charCodeAt(0));
      const json = await readBounded(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate')));
      return validate(JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(json)));
    } catch (_) { throw invalid(); }
  }

  function capture(objects, metric, palette) {
    if (objects.length > MAX_OBJECTS) throw tooLarge();
    return validate([metric ? 1 : 0, objects.map(object => {
      const angle = Math.round((object.rotation - object.defaultRotation) * 1800 / Math.PI);
      return [object.product.id, object.size.index, Math.round(object.x * 100), Math.round(object.y * 100),
        ((angle % 3600) + 5400) % 3600 - 1800, object.mirrored ? 1 : 0, palette.indexOf(object.color)];
    })]);
  }

  globalThis.CompareShare = {capture, encode, decode, MAX_URL_LENGTH};
})();
