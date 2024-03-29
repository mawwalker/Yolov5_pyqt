# Author: Deng Shi Ma
# Date: 2021.03.25
import sys
import os
import queue
import cv2
import yaml
import shutil
import time
from multiprocessing import Queue, Process
import multiprocessing
# import torch
# from torch.multiprocessing import Process
# from multiprocessing import Process, Queue
# from tqdm import tqdm
from PyQt5 import QtGui
from PyQt5.QtWidgets import QApplication, QMainWindow, QFileDialog, QMessageBox
from PyQt5.QtCore import QThread, QEvent
from ui.mainUI import Ui_MainWindow
from ui.trainParasMain import TrainWindow
import qdarkstyle

from funlibs import init_dir, print_txt, cropimage_overlap, concat_image, showImages
from yoloThreads import EnlightenWork, DetectThread, OutputThread, FogThread
from algorithms.Yolov5 import Yolov5_train

import platform


# os compatible for MacOSX
# refer to https://github.com/keras-team/autokeras/issues/368
if platform.system().lower() == "darwin":
    class SharedCounter(object):
        """ A synchronized shared counter.

        The locking done by multiprocessing.Value ensures that only a single
        process or thread may read or write the in-memory ctypes object. However,
        in order to do n += 1, Python performs a read followed by a write, so a
        second process may read the old value before the new one is written by the
        first process. The solution is to use a multiprocessing.Lock to guarantee
        the atomicity of the modifications to Value.

        This class comes almost entirely from Eli Bendersky's blog:
        http://eli.thegreenplace.net/2012/01/04/shared-counter-with-pythons-multiprocessing/

        """

        def __init__(self, n = 0):
            self.count = multiprocessing.Value('i', n)

        def increment(self, n = 1):
            """ Increment the counter by n (default = 1) """
            with self.count.get_lock():
                self.count.value += n

        @property
        def value(self):
            """ Return the value of the counter """
            return self.count.value


    class Queue(queue.Queue):
        """ A portable implementation of multiprocessing.Queue.

        Because of multithreading / multiprocessing semantics, Queue.qsize() may
        raise the NotImplementedError exception on Unix platforms like Mac OS X
        where sem_getvalue() is not implemented. This subclass addresses this
        problem by using a synchronized shared counter (initialized to zero) and
        increasing / decreasing its value every time the put() and get() methods
        are called, respectively. This not only prevents NotImplementedError from
        being raised, but also allows us to implement a reliable version of both
        qsize() and empty().

        """

        def __init__(self, *args, **kwargs):
            super(Queue, self).__init__(*args, **kwargs)
            self.size = SharedCounter(0)

        def put(self, *args, **kwargs):
            self.size.increment(1)
            super(Queue, self).put(*args, **kwargs)

        def get(self, *args, **kwargs):
            self.size.increment(-1)
            return super(Queue, self).get(*args, **kwargs)

        def qsize(self):
            """ Reliable implementation of multiprocessing.Queue.qsize() """
            return self.size.value

        def empty(self):
            """ Reliable implementation of multiprocessing.Queue.empty() """
            return not self.qsize()



class MyWindow(QMainWindow, Ui_MainWindow):
    def __init__(self, parent=None):
        super(MyWindow, self).__init__(parent)
        self.setupUi(self)
        # 读取配置文件
        with open('config.yaml', 'r', encoding='UTF-8') as f:
            self.config = yaml.load(f, Loader=yaml.FullLoader)

        # 设置按钮响应函数**************************************
        # 将按钮响应函数整合到一个函数，方便修改管理，修改option字典对应函数即可
        # 对应规则即，根据对应按钮或者菜单选项的名字，查字典，获取对应函数
        self.treeWidget.itemClicked.connect(lambda:
                                            self.onclick(self.treeWidget.currentItem().text(0)))
        self.options = {'打开文件': 'openfile', '关闭文件': 'reset', '大图切割': 'cropimage',
                        '亮度增强（EnlightenGAN）': 'enlighten', '图像去雾': 'ridfog', '小图合并': 'concat',
                        '选择模型': 'choosemodel', '参数设置': 'selectdata', '开始训练': 'train',
                        '终止训练': 'stoptrain', '执行检测': 'detection', '模型剪枝': 'lightweight',
                        '重置默认': 'default', '批量检测': 'detectdir'}
        self.action.triggered.connect(self.exitprogram)  # 退出程序

        # 当需要传入参数时，要使用lambda表达式函数
        self.pushButton.clicked.connect(lambda: self.onclick('大图切割'))
        self.pushButton_2.clicked.connect(lambda: self.onclick('执行检测'))
        # 清空输出
        self.pushButton_3.clicked.connect(lambda: self.textBrowser.clear())
        # 训练窗口
        self.pushButton_4.clicked.connect(self.changetrain)
        self.pushButton_5.clicked.connect(self.showtrain)

        # 缩放按钮
        self.pushButton_6.clicked.connect(lambda: self.scrollResize(1.2))
        self.pushButton_7.clicked.connect(lambda: self.scrollResize(0.8))

        # 模型选择菜单*********************************************
        self.models_dict = {'深度学习目标检测识别模型(RGB)': 'weights/RGBbest.pt',
                            '基于样本迁移的目标检测识别模型(RGB)': 'weights/RGBbest.pt',
                            '基于特征迁移的目标检测识别模型(PAN)': 'weights/PANbest.pt',
                            '基于模型迁移的目标检测识别模型(IR)': 'weights/IRbest.pt'}

        # 按键选择对应模型权重
        self.comboBox.currentIndexChanged.connect(
            lambda: self.changemodel(self.models_dict[self.comboBox.currentText()])
        )

        # 图片切割行列数
        self.rows = 2   # rows
        self.colums = 2   # columns

        # 主线程， 判断任务队列是否存在待执行的任务
        self.th = OutputThread(data=self)
        # 文本重定向至GUI控件
        self.th.signalForText.connect(self.outputWritten)
        self.th.judge.connect(self.judge_thread)
        # 将控制台文本重定向至GUI控件中
        # sys.stdout = self.th
        # sys.stderr = self.th
        self.th.start()

        # 训练参数配置子窗口
        self.train_win = TrainWindow()

        # 窗口，变量重置
        self.reset(start=True)

        # 线程队列
        # self.qmain = queue.Queue()
        self.qmain = Queue()

        # 获取训练输出消息使用的队列
        self.train_output_q = Queue()

        # 计算线程执行时间的队列
        self.time_queue = Queue()

        # 当前任务清空
        self.current_task = None

    def judge_thread(self):
        '''
        主线程后台一直在执行的函数
        判断任务队列中是否存在待执行的任务
        同时只能有一个任务在执行（训练程序单独运行）
        并且获取训练程序中的输出，展示进度条等
        '''
        # 将训练进程中的输出，显示到GUI控件中
        '''
        训练返回的消息，有多种类型
        1. 当返回为dict字典类型时，说明，返回的是训练的batchs，epoch参数
        2. 返回一个列表，并且长度为2， 说明返回的是每次迭代的情况，在训练中
        3. 除此之外，为一般消息，直接显示就好
        '''
        if not self.train_output_q.empty():
            s = self.train_output_q.get()
            # 开始训练之后，返回训练进度条信息，和当前epoch
            if isinstance(s, dict):
                batchs = s['batchs']
                i = s['i']
                epoch = s['epoch']
                self.train_win.progressBar.setMaximum(batchs)
                self.train_win.progressBar.setValue(i)
                self.progressBar.setValue(epoch)
            # 返回一个列表的时候，代表已经开始训练，具体见训练函数中的返回值，
            # 此处需要不断更新当前训练的数据，所以需要删除之前的最后一行，
            # 再添加新一行
            elif len(s) == 2:
                # 设置光标，删除最后一行
                storeCursorPos = self.train_win.textBrowser.textCursor()
                self.train_win.textBrowser.moveCursor(QtGui.QTextCursor.End,
                                                      QtGui.QTextCursor.MoveAnchor)
                self.train_win.textBrowser.moveCursor(QtGui.QTextCursor.StartOfLine,
                                                      QtGui.QTextCursor.MoveAnchor)
                self.train_win.textBrowser.moveCursor(QtGui.QTextCursor.End,
                                                      QtGui.QTextCursor.KeepAnchor)
                self.train_win.textBrowser.textCursor().removeSelectedText()
                self.train_win.textBrowser.textCursor().deletePreviousChar()
                self.train_win.textBrowser.setTextCursor(storeCursorPos)

                # 添加新一行，达到刷新显示数据的目的
                s = s[0]
                self.train_win.textBrowser.append(s)
            else:
                self.train_win.textBrowser.append(s)
        # 主线程消息队列
        # 会在程序运行中，依次存储：操作类型，开始时间，结束时间，当得到结束时间时，会触发该判断
        # 当消息队列中存在3个时，表示一个线程已经执行完成，需要显示线程执行的时间
        if self.time_queue.qsize() == 3:
            print('完成')
            string = self.time_queue.get()
            start = self.time_queue.get()
            end = self.time_queue.get()
            self.textBrowser.append(string + ('%.4f') % (end-start) + 's')
        # 处理任务队列
        # 任务队列为空
        if self.qmain.empty():
            return
        # 执行队列中的任务
        else:
            self.current_task = self.qmain.get()
            self.current_task.start()

    def changemodel(self, model_weight):
        '''
        修改后面检测使用的权重文件
        :param: model_weight: 检测使用的权重文件, str, 路径
        '''
        self.model_weight = model_weight
        print(self.model_weight)

    def choosemodel(self):
        '''
        选择已训练好的权重文件
        '''
        selected_filter = "Models (*.pt);;All Files(*)"
        FileName, FileType = QFileDialog.getOpenFileName(self, "打开图片", "", selected_filter)
        # 需要加判断，防止选择时，未正确选择而取消了，导致变量为空
        if FileName != '':
            self.model_weight = FileName.replace(os.getcwd()+'/', '', 1)
            # print(self.model_weight)

    def reset(self, start=False):
        '''
        重置函数

        重置变量，初始化, 输入：

        :param: start: 决定是否重置重叠率，模型等变量参数, bool
                    True: 重置重叠率，模型等所有参数
                    False：不重置上述参数，仅关闭正在显示的图片
        '''
        # 左侧树展开
        self.treeWidget.expandToDepth(0)

        # 结果展示列表
        self.tableWidget.setRowCount(0)

        # 共享变量的重置
        self.images = []
        self.sourceimage = ''
        self.result = []

        # 控制台日志清空
        self.textBrowser.clear()
        # self.textBrowser_2.clear()

        # 清空目录
        init_dir(self.config['output_dir'])
        init_dir(self.config['source'])
        init_dir(self.config['temp'])
        init_dir(self.config['result'])

        # 为scrollArea添加事件响应函数
        self.last_time_ymove = 0
        self.last_time_xmove = 0
        self.scrollArea.installEventFilter(self)

        # 设置分割时的行列数
        self.spinBox.setValue(self.colums)
        self.spinBox_2.setValue(self.rows)
        # 数值框改变，触发函数
        self.spinBox.valueChanged.connect(self.changevalue)
        self.spinBox_2.valueChanged.connect(self.changevalue)

        # 关闭文件不执行，重置会执行
        if start:
            # 切割重叠率重置
            self.doubleSpinBox.setValue(0.0)
            self.doubleSpinBox_2.setValue(0.0)

            # 设置默认模型
            self.comboBox.setCurrentText('深度学习目标检测识别模型(RGB)')
            self.model_weight = self.models_dict['深度学习目标检测识别模型(RGB)']
        else:
            # 对应关闭文件选项
            showImages(self.gridLayout_2, self.colums, self.rows, [])

    def default(self):
        '''
        重置变量，模型等参数
        '''
        self.reset(start=True)

    def exitprogram(self):
        '''
        退出程序
        '''
        try:
            # 退出之前先关闭训练进程
            self.train_pro.kill()
            self.train_pro.close()
        except Exception as e:
            print(e)
        exit()

    def outputWritten(self, text):
        '''
        重定向文本输出
        '''
        cursor = self.textBrowser.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        cursor.insertText(text)
        self.textBrowser.setTextCursor(cursor)
        self.textBrowser.ensureCursorVisible()

    def scrollResize(self, rate):
        '''
        图片预览窗口缩放
        '''
        width = self.widget_10.width() * rate
        height = self.widget_10.height() * rate
        self.widget_10.resize(int(width), int(height))

    def changevalue(self):
        '''
        设置行列的控件，实时监听修改事件
        '''
        self.colums = self.spinBox.value()
        self.rows = self.spinBox_2.value()

    def onclick(self, option):
        '''
        根据按钮名字，执行对应响应函数
        '''
        # 当前有任务执行，点击显示警告弹窗，并返回
        if self.current_task is not None:
            QMessageBox.warning(self, "标题", "请等待后台程序执行完毕")
            return
        try:
            # 根据按钮名称获取响应函数，并执行对应函数
            getattr(self, self.options[option])()
        except Exception as e:
            print(e)

    def openfile(self):
        '''
        打开一张图片
        '''
        self.tableWidget.setRowCount(0)
        # 选择打开选项，图片格式，所有文件，括号中使用匹配符号
        selected_filter = "Images (*.png *.jpg *.JPEG);;All Files(*)"
        imgName, imgType = QFileDialog.getOpenFileName(self, "打开图片", "", selected_filter)
        # 当未选中图片取消时，直接返回
        if len(imgName) == 0:
            return
        else:
            '''
            将sourceimage和images都设置为刚才打开的图片,
            调用showImages函数显示
            '''
            self.reset(start=False)
            # 获取图片的相对路径
            imgName = imgName.replace(os.getcwd()+'/', '', 1)
            # 清空source路径，并将图片移动至source中
            init_dir(self.config['source'])
            init_dir(self.config['temp'])
            init_dir(self.config['result'])
            self.sourceimage = shutil.copy(imgName, self.config['source'])
            image = shutil.copy(self.sourceimage, self.config['temp'])

            self.images = [image]
            showImages(self.gridLayout_2, self.colums, self.rows, self.images)

    def detectdir(self):
        '''
        批量检测，检测一个文件夹
        '''
        select = QFileDialog.getExistingDirectory(self, "选择文件夹", "")
        # 未选择路径或者路径无效
        if len(select) == 0 or os.path.exists(select) is False:
            return
        directory = select.replace(os.getcwd()+'/', '', 1)
        # 批量检测输出结果与普通检测不同，分开放
        outputdir = self.config['batchoutput']
        # 使用多线程
        self.detectdir_th = DetectThread([directory, self.model_weight, outputdir])

        self.detectdir_th.finished.connect(self.detectdir_result)
        self.qmain.put(self.detectdir_th)
        self.time_queue.put('批量检测耗时: ')
        self.time_queue.put(time.time())

    def detectdir_result(self):
        '''
        批量检测完毕，告知结果
        '''
        self.time_queue.put(time.time())
        self.textBrowser.append('批量检测结果存放在: ' + self.config['batchoutput'])
        self.current_task = None

    def cropimage(self):
        '''
        切图函数
        无需传参，根据self.images列表进行切割
        无返回值，切割结果赋值给self.images（列表形式），并显示
        '''
        # 如果当前图片不是一张，则为误操作
        if len(self.images) != 1 and len(self.result) > 0:
            QMessageBox.warning(self, "标题", "当前无需进行切图操作")
            return
        else:
            self.images = [self.sourceimage]
        # 获取设置的重叠率
        overlap_h = self.doubleSpinBox.value()
        overlap_v = self.doubleSpinBox_2.value()
        # 调用切割图片的函数
        result = cropimage_overlap(self.images[0], self.colums,
                                   self.rows, self.config['temp'],
                                   h=overlap_h, v=overlap_v)
        # 第一个返回为切割后的小图路径列表，第二个为原大图画线的结果
        self.images = result[0]
        show_name = result[1]
        # 显示图片
        # showImages(self.gridLayout_2, self.colums, self.rows, self.images)
        showImages(self.gridLayout_2, self.colums, self.rows, [show_name])
        self.tableWidget.setRowCount(0)

    def enlighten(self):
        '''
        self.images(增强前)  ->  self.images（增强后）
        showImages 显示
        '''
        if len(self.images) == 0:
            QMessageBox.warning(self, "标题", "请先选择图片")
            return
        # 后台线程执行亮度增强函数
        # 新建一个线程和QObject，并使用moveToThread实现多线程
        self.lighten_th = QThread()
        self.lighten_wk = EnlightenWork(self.images)
        self.lighten_wk.moveToThread(self.lighten_th)

        # 结束信号绑定函数
        self.lighten_th.started.connect(self.lighten_wk.run)
        self.lighten_wk.finished.connect(self.lighten_th.quit)
        self.lighten_wk.finished.connect(self.lighten_wk.deleteLater)
        self.lighten_th.finished.connect(self.lighten_th.deleteLater)

        self.lighten_wk.finished.connect(self.enlighten_finished)

        self.qmain.put(self.lighten_th)
        # 当前时间入队列
        self.time_queue.put('亮度增强耗时: ')
        self.time_queue.put(time.time())

    def enlighten_finished(self):
        '''
        当亮度增强算法运行结束后，执行的函数
        显示增强后的图片
        '''
        lowlight_result = self.config['temp']
        image_names = [(lowlight_result + '/' + name) for name in os.listdir(lowlight_result)]
        image_names.sort()
        self.images = image_names
        print('enlighten result:', self.images)
        showImages(self.gridLayout_2, self.colums, self.rows, self.images)
        self.tableWidget.setRowCount(0)
        self.current_task = None
        self.time_queue.put(time.time())

    def ridfog(self):
        '''
        对应目录解释在__init__.py中
        self.images -> self.images
        增强前  ->  增强后
        '''
        paths = self.config['fog_dir']
        target = paths[0]
        out_result = self.config['fog_outdoor']
        init_dir(target)
        init_dir(out_result)
        if len(self.images) == 0:
            return
        # 将图片移动到去雾算法所使用的路径中
        for file in self.images:
            try:
                shutil.copy(file, target)
            except IOError as e:
                print("Unable to copy file. %s" % e)
            except Exception:
                print("Unexpected error:", sys.exc_info())
        # 多线程
        self.ridfog_th = FogThread(paths)
        self.ridfog_th.finished.connect(self.ridfog_result)
        self.qmain.put(self.ridfog_th)
        self.time_queue.put('去雾耗时: ')
        self.time_queue.put(time.time())

    def ridfog_result(self):
        '''
        去雾进程完成后，触发显示结果的函数
        读取结果文件，并显示
        '''
        out_result = self.config['fog_outdoor']
        image_names = [(out_result + '/' + name) for name in os.listdir(out_result)]
        image_names.sort()
        init_dir(self.config['temp'])
        results = []
        # 将去雾的到的图片移动到统一的中间存储目录中
        for image in image_names:
            results.append(shutil.copy(image, self.config['temp']))
        self.images = results
        print('remove fog result:', self.images)
        showImages(self.gridLayout_2, self.colums, self.rows, self.images)
        self.tableWidget.setRowCount(0)
        self.time_queue.put(time.time())
        self.current_task = None

    def concat(self):
        '''
        图片拼接函数

        图片拼接有两种情况，
        1. 原图未检测，切割后直接拼接
        2. 原图切割后，对检测结果进行拼接
        目前的检测后拼接方案为“假拼接”，实际并没有对检测后的小图拼接，
        而是直接使用原大图进行检测后再拼接
        '''
        # 检测结果图
        n = len(self.result)
        # 待检测原图
        m = len(self.images)
        # 待检测的小图均已检测，拼接检测结果
        if m > 1 and n > 1:
            concat_path = self.config['result']
            # 使用拼接原切割图，再使用大图检测的“假拼接方式”
            # concat_path = self.config['temp']
        # 小图未检测，拼接回原图
        elif m > 1:
            concat_path = self.config['temp']
        # 图片不需要拼接
        else:
            return
        image_names = []
        for name in os.listdir(concat_path):
            # 排除txt文件
            if name[-4:] != '.txt':
                image_names.append(concat_path + '/' + name)
        image_names.sort()
        # 根据重叠率合并图片
        img_result = concat_image(image_names, self.colums, self.rows,
                                  overlap_h=self.doubleSpinBox.value(),
                                  overlap_v=self.doubleSpinBox_2.value())
        # 清空原来路径，并将新图保存至原来路径
        init_dir(concat_path)
        cv2.imwrite(image_names[0], img_result)
        # 如果是拼接原图，则self.images还是拼接后的图
        # 如果是拼接检测结果，则self.result应改变为拼接结果
        if concat_path == self.config['temp']:
            self.images = [image_names[0]]
        else:
            self.result = [image_names[0]]

        showImages(self.gridLayout_2, self.colums, self.rows, [image_names[0]])

    def detection(self):
        '''
        根据单张图片还是多张图片，
        传入图片名或者路径
        '''
        if len(self.images) == 0:
            QMessageBox.warning(self, "错误", "当前无可以检测的图片")
            return
        detect_dir = os.path.dirname(self.images[0])
        self.detect_th = DetectThread([detect_dir, self.model_weight])

        self.detect_th.finished.connect(self.detect_result)
        self.qmain.put(self.detect_th)
        self.time_queue.put('检测耗时: ')
        self.time_queue.put(time.time())

    def detect_result(self, result):
        '''
        显示检测结果的函数
        '''
        self.result = result
        init_dir(self.config['result'])
        result0 = []
        # 将图片移动到存储结果文件的目录中,并显示
        for image in self.result:
            result0.append(shutil.copy(image, self.config['result']))
        self.result = result0
        showImages(self.gridLayout_2, self.colums, self.rows, self.result)
        print_txt(self.tableWidget, self.config['targets'], result)
        self.time_queue.put(time.time())
        self.detect_th.deleteLater()
        self.current_task = None

    def selectdata(self):
        '''
        训练参数设置
        '''
        self.train_win.show()
        self.train_win.exec_()

    def changetrain(self):
        '''
        训练进程控制按钮的响应函数,变换按钮状态
        '''
        if self.pushButton_4.text() == '开始训练':
            self.train()
        else:
            self.stoptrain()

    def showtrain(self):
        '''
        显示训练程序的窗口
        '''
        self.train_win.show()
        self.train_win.exec()

    def stoptrain(self):
        '''
        停止训练函数
        '''
        try:
            self.train_pro.kill()
            self.train_pro.close()
        except Exception as e:
            print(e)
        self.pushButton_4.setText('开始训练')
        print('stoptrain')

    def train(self):
        '''
        开始训练
        '''
        # 设置界面设置训练参数
        dataset = self.train_win.dataset
        weights = self.train_win.weights
        batchsize = self.train_win.batchsize
        epochs = self.train_win.epochs
        print("", dataset, weights, batchsize, epochs)
        try:
            del self.train_pro
        except Exception as e:
            print(e)
        self.train_pro = Process(target=Yolov5_train.train0, args=(
            'train.yaml', 'models/yolov5s.yaml', '', batchsize, epochs, self.train_output_q,
        ))
        self.train_win.textBrowser.clear()
        self.train_pro.start()
        self.train_win.show()
        self.pushButton_4.setText('停止训练')
        self.progressBar.setMaximum(epochs - 1)
        self.train_win.label_3.setText('epoch:0/' + str(epochs-1))

    def lightweight(self):
        '''
        模型剪枝函数，，，害，假的
        '''
        self.model_weight = 'weights/Fast.pt'

    def eventFilter(self, obj, event):
        '''
        设置鼠标可以拖动图片框
        '''
        if event.type() == QEvent.MouseMove:
            if self.last_time_ymove == 0:
                self.last_time_ymove = event.pos().y()
            if self.last_time_xmove == 0:
                self.last_time_xmove = event.pos().x()
            distance_y = self.last_time_ymove - event.pos().y()
            distance_x = self.last_time_xmove - event.pos().x()
            self.scrollArea.verticalScrollBar().setValue(
                self.scrollArea.verticalScrollBar().value() + distance_y
            )
            self.scrollArea.horizontalScrollBar().setValue(
                self.scrollArea.horizontalScrollBar().value() + distance_x
            )
            self.last_time_ymove = event.pos().y()
            self.last_time_xmove = event.pos().x()
        elif event.type() == QEvent.MouseButtonRelease:
            self.last_time_ymove = 0
            self.last_time_xmove = 0
        # return QWidget.eventFilter(self, source, event)
        return super(MyWindow, self).eventFilter(obj, event)

    def closeEvent(self, event):
        sys.exit(app.exec_())


if __name__ == '__main__':
    multiprocessing.set_start_method('spawn')
    multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    myWin = MyWindow()
    # 使用qdarkstyle风格
    app.setStyleSheet(qdarkstyle.load_stylesheet(qt_api='pyqt5'))
    myWin.show()
    sys.exit(app.exec_())
