import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from kwin_layout import script_source,arrange_windows

class BorderlessGridTests(unittest.IsolatedAsyncioTestCase):
 async def test_kwin_confirms_borderless_and_geometry_only_after_distinct_window_preflight(self):
  plans=[dict(stream_id='a',x=0,y=0,width=500,height=400)]
  page=SimpleNamespace(title=AsyncMock(return_value='Game'),evaluate=AsyncMock())
  result={'success':True,'windows':[{'geometry':dict(x=0,y=0,width=500,height=400),'borderless':True}]}
  with patch('kwin_layout.run_script',AsyncMock(return_value=result))as run,patch('kwin_layout.asyncio.sleep',AsyncMock()):
   self.assertTrue((await arrange_windows({'a':SimpleNamespace(page=page)},plans,borderless=True))['success'])
   self.assertEqual(run.await_args_list[1].kwargs,{'borderless':True})
   self.assertEqual(run.await_args_list[0].kwargs,{'dry_run':True})
   result['windows'][0]['borderless']=False
   self.assertFalse((await arrange_windows({'a':SimpleNamespace(page=page)},plans,borderless=True))['success'])
  self.assertEqual(page.evaluate.await_args.args,('title => document.title = title','Game'))
 def test_kwin_defaults_do_not_remove_unrelated_decorations(self):
  self.assertIn('const borderless = null',script_source([], 'fixture'))
  self.assertIn('const borderless = true',script_source([], 'fixture',borderless=True))
  self.assertIn('const borderless = false',script_source([], 'fixture',borderless=False))

class X11DecorationTests(unittest.TestCase):
 def test_grid_focus_grid_and_decorated_restore_preserve_original_hints(self):
  import sys
  from unittest.mock import Mock
  import x11_windows
  atoms={};props={}
  def atom(name):return atoms.setdefault(name,len(atoms)+1)
  window=SimpleNamespace(get_full_property=lambda key,_:SimpleNamespace(value=props[key])if key in props else None,change_property=lambda key,_type,_bits,value:props.__setitem__(key,list(value)),delete_property=lambda key:props.pop(key,None))
  dialog=atom('_NET_WM_WINDOW_TYPE_DIALOG');normal=atom('_NET_WM_WINDOW_TYPE_NORMAL');type_key=atom('_NET_WM_WINDOW_TYPE');motif=atom('_MOTIF_WM_HINTS');state=atom('_NET_WM_STATE');full=atom('_NET_WM_STATE_FULLSCREEN')
  props[type_key]=[dialog];props[motif]=[2,0,1,0,0]
  def send(event,**_):
   if event.client_type==atom('_NET_WM_STATE'):
    values=props.setdefault(state,[])
    for value in event.data[1][1:3]:
     if not value:continue
     if event.data[1][0]==1 and value not in values:values.append(value)
     elif event.data[1][0]==0 and value in values:values.remove(value)
  display=SimpleNamespace(screen=lambda:SimpleNamespace(root=SimpleNamespace(send_event=send)),create_resource_object=lambda *_:window,intern_atom=atom,sync=lambda:None,close=lambda:None)
  fake=SimpleNamespace(X=SimpleNamespace(AnyPropertyType=0,SubstructureRedirectMask=1,SubstructureNotifyMask=2),Xatom=SimpleNamespace(ATOM=4),protocol=SimpleNamespace(event=SimpleNamespace(ClientMessage=lambda **kw:SimpleNamespace(**kw))))
  with patch.dict(sys.modules,{'Xlib':fake,'Xlib.display':SimpleNamespace(Display=lambda:display)}),patch.object(x11_windows.time,'sleep'),patch.object(x11_windows,'window_matches',return_value=True):
   bounds=dict(x=0,y=0,width=1000,height=600)
   x11_windows.move_window(42,bounds,fullscreen=False,borderless=True)
   self.assertEqual(props[motif],[2,0,0,0,0]);self.assertNotIn(full,props.get(state,[]))
   x11_windows.move_window(42,bounds,fullscreen=True,borderless=True)
   self.assertEqual(props[type_key],[normal]);self.assertIn(full,props[state])
   x11_windows.move_window(42,bounds,fullscreen=False,borderless=True)
   self.assertEqual(props[type_key],[dialog]);self.assertNotIn(full,props[state]);self.assertEqual(props[motif],[2,0,0,0,0])
   x11_windows.move_window(42,bounds,fullscreen=False,borderless=False)
   self.assertEqual(props[motif],[2,0,1,0,0]);self.assertEqual(props[type_key],[dialog])
