"""GPU identity selection must survive numbering changes and reject unsafe fallbacks."""
import json
import unittest
from unittest import mock
from local_runtime.vulkan_device import nvidia_environment, select_device, prepare_engine


class VulkanDeviceTests(unittest.TestCase):
    def test_filter_is_child_only_and_removes_stale_indices(self):
        parent={'GGML_VK_VISIBLE_DEVICES':'1','VK_DRIVER_FILES':'old.json','PATH':'normal'}
        child=nvidia_environment(parent)
        self.assertEqual(parent['GGML_VK_VISIBLE_DEVICES'],'1')
        self.assertNotIn('GGML_VK_VISIBLE_DEVICES',child)
        self.assertNotIn('VK_DRIVER_FILES',child)
        self.assertEqual(child['PATH'],'normal')
        self.assertEqual(child['VK_LOADER_DRIVERS_SELECT'],'nv-vk64.json,nvidia*.json')

    def test_unfiltered_devices_rejected_in_either_order(self):
        for names in [('Intel UHD 770','NVIDIA RTX 3060'),('NVIDIA RTX 3060','Intel UHD 770')]:
            devices=[{'name':f'Vulkan{i}','description':name,'type':1 if 'NVIDIA' in name else 2} for i,name in enumerate(names)]
            with self.assertRaises(RuntimeError):
                select_device(devices)
        for devices in ([],[{'name':'CPU','description':'CPU','type':0}],
                        [{'name':'Vulkan0','description':'Intel UHD 770','type':2}]):
            with self.assertRaises(RuntimeError):
                select_device(devices)

    def test_cold_wake_probes_every_time_and_uses_verified_device(self):
        device={'name':'Vulkan0','description':'NVIDIA GeForce RTX 3060','type':1}
        command=['/app/cosyvoice-server.exe','--backend','nvidia-vulkan']
        result=mock.Mock(returncode=0,stdout=json.dumps([device]))
        with mock.patch('local_runtime.vulkan_device.subprocess.run',return_value=result) as run:
            for _ in range(2):
                argv,env,selected=prepare_engine(command,{})
                self.assertEqual(argv[-1],'Vulkan0')
                self.assertEqual(selected,device)
                self.assertIn('VK_LOADER_DRIVERS_SELECT',env)
            self.assertEqual(run.call_count,2)
        self.assertEqual(command[-1],'nvidia-vulkan')


if __name__=='__main__':
    unittest.main()
