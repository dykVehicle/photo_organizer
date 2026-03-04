# -*- coding: utf-8 -*-
"""测试新模型对之前误检文件的表现"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nsfw_detector import NsfwDetector
d = NsfwDetector(threshold=0.5)

test_files = [
    r"H:\All_相册_20260225_V2\All_2_目标设备_相机照片\2022-Q2-M4-5-6_Canon EOS RP\IMG_0872.JPG",
    r"H:\All_相册_20260225_V2\All_2_目标设备_相机照片\2019-Q3-M7-8-9_Canon EOS 550D\IMG_2050.JPG",
    r"H:\All_相册_20260225_V2\All_1_目标设备_手机照片\2018-Q2-M4-5-6_Xiaomi MI 6\IMG_20180512_121052.jpg",
    r"H:\All_相册_20260225_V2\All_1_目标设备_手机照片\2023-Q3-M7-8-9_HUAWEI ANA-AN00\IMG_20230726_142957.jpg",
    r"H:\All_相册_20260225_V2\All_1_目标设备_手机照片\2012-Q3-M7-8-9_Xiaomi MI-ONE Plus\C360_2012-09-03-20-40-49.jpg",
    r"H:\All_相册_20260225_V2\All_1_目标设备_手机照片\2019-Q2-M4-5-6_Apple iPhone 7\2019_06_20_23_34_IMG_5203.JPG",
    r"H:\All_相册_20260225_V2\All_2_目标设备_相机照片\2020-Q2-M4-5-6_Canon EOS 550D\IMG_4158.JPG",
    r"H:\All_相册_20260225_V2\All_1_目标设备_手机照片\2022-Q3-M7-8-9_HUAWEI ANA-AN00\IMG_20220921_123432.jpg",
]

for f in test_files:
    if os.path.isfile(f):
        score = d.predict_image(f)
        tag = "NSFW" if score >= 0.5 else "CLEAN"
        print("%.4f [%5s] %s" % (score, tag, os.path.basename(f)))
    else:
        print("NOT FOUND: %s" % os.path.basename(f))
