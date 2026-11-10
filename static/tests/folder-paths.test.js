const assert = require('node:assert/strict');
const {indexFolderFiles} = require('../folder-paths.js');

const file = webkitRelativePath => ({webkitRelativePath});
const regular = [file('root/docs/a.md'), file('root/images/café.png')];
assert.equal(indexFolderFiles(regular).get('root/images/café.png'), regular[1]);
assert.throws(() => indexFolderFiles([file('root/images/café.png'), file('root/images/cafe\u0301.png')]), /无法区分的同名路径/);
assert.throws(() => indexFolderFiles([file('root/a.png'), file('root/a.png')]), /无法区分的同名路径/);
assert.equal(indexFolderFiles([file('root/a/a.png'), file('root/b/a.png')]).size, 2);
