const {test}=require('node:test');
const assert=require('node:assert/strict');
const katex=require('../katex/katex.min.js');

test('common LaTeX math renders and unsupported commands fail safely',()=>{
  for(const tex of [String.raw`\frac{a}{b}`,String.raw`\sum_{i=1}^{n} x_i`,String.raw`\begin{cases} x^2 & x>0 \\ 0 & x\le 0 \end{cases}`,String.raw`\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}`,String.raw`\begin{align} a &= b \\ c &= d \end{align}`]){
    assert.match(katex.renderToString(tex,{displayMode:true,throwOnError:true,trust:false}),/class="katex/);
  }
  assert.throws(()=>katex.renderToString(String.raw`\unsupportedExample{x}`,{throwOnError:true,trust:false}));
});
