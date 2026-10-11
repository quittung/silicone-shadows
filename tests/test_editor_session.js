const assert = require('node:assert/strict');
const {EditorSession} = require('../static/editor-session.js');
const session = new EditorSession({sizes:['Small','Medium','Large'], outlines:[
  {id:'', sizes:[], mode:'svg', original_id:'', state:{rating:'good', main_length:{start:[1,8],end:[1,2]}}},
]});
// Assigning the general outline to sizes keeps its vector basis and local edits.
session.outline('').paint = {local:true};
session.changeVariant({action:'add',id:'small',move_from:'',sizes:['Small']});
assert.equal(session.outline('small').original_id,'');
assert.deepEqual(session.outline('small').paint,{local:true});
assert.equal(session.collection().general,false);
session.changeVariant({action:'general'});
assert.equal(session.collection().general,true);
session.changeVariant({action:'add',id:'medium',sizes:['Medium']});
session.outline('medium').state.rating = 'unusable';
assert.throws(() => session.changeVariant({action:'assign',id:'small',sizes:['Medium']}), /already belongs/);
assert.deepEqual(session.outline('small').sizes,['Small']);
assert.throws(() => session.changeVariant({action:'add',id:'unknown',sizes:['Unknown']}), /Unknown size/);
session.changeVariant({action:'remove',id:''});
session.changeVariant({action:'remove',id:'small'});
assert.equal(session.outline('medium').state.rating,'unusable');
assert.throws(() => session.changeVariant({action:'remove',id:'medium'}), /at least one/);
// States are independent copies of the baseline; local operations never use fetch.
assert.equal(session.document.outlines[0].state.rating,'good');
console.log('Editor session invariants passed.');
