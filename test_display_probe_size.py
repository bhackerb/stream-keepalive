import unittest
from display_probe import bind_displays

class MovedMonitor(unittest.TestCase):

    def test_moved_monitor_binds_by_unique_size(self):
        outputs=[{'name':'DP-6','connected':True,'enabled':True,'x':1707,'y':0,'width':3440,'height':1440},
             {'name':'DP-4','connected':True,'enabled':True,'x':5147,'y':288,'width':2048,'height':1152},
             {'name':'eDP-1','connected':True,'enabled':True,'x':0,'y':373,'width':1707,'height':1067}]
        configured={'widescreen':{'x':1707,'y':0,'width':3440,'height':1440},'secondary':{'x':5147,'y':144,'width':2048,'height':1152},'laptop':{'x':0,'y':186,'width':1707,'height':1067}}
        displays,avail=bind_displays(configured,outputs)
        assert avail=={'widescreen':'connected','secondary':'connected','laptop':'connected'}
        assert displays['secondary']['y']==288 and displays['secondary']['output']=='DP-4'


class SecondaryNoThreeGrid(unittest.TestCase):
    def test_three_streams_use_quarters(self):
        from display_layout import plan_layout
        r={'secondary':{'x':0,'y':0,'width':2048,'height':1152}}
        cells=plan_layout(r,'secondary','grid',['a','b','c'])
        self.assertEqual({(c['width'],c['height'])for c in cells},{(1024,576)})
    def test_manual_three_preset_migrates_to_four(self):
        from manual_layout import validate_manual_layout
        r={'secondary':{'x':0,'y':0,'width':2048,'height':1152,'max_streams':4}}
        self.assertEqual(validate_manual_layout({'presets':{'secondary':3},'assignments':[]},r)['presets'],{'secondary':4})

if __name__=='__main__':unittest.main()
