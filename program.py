# -*- coding: utf-8 -*-
"""
Программа для управления умным освещением через Arduino.
Реализует приём телеметрии (уровень сигнала, огибающая, состояние реле),
отображение графиков, настройку параметров фильтрации и ручное управление.
Использует протокол JSON через последовательный порт.
"""

import sys
import json
import serial
import serial.tools.list_ports
from datetime import datetime
from collections import deque
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
import pyqtgraph as pg


class SerialWorker(QThread):
    """
    Поток для работы с последовательным портом.
    Асинхронно читает данные от Arduino, парсит JSON и отправляет сигналы.
    Также отправляет команды (конфигурация, ручное управление).
    """

    # Сигналы для взаимодействия с основным потоком GUI
    telemetry_received = pyqtSignal(dict)  # Новая телеметрия: {"a": ADC, "e": envelope, "s": state, "r": relay}
    status_message = pyqtSignal(str, str)  # (текст, уровень: INFO/ERROR/SUCCESS/ACTION)
    connection_changed = pyqtSignal(bool)  # True - соединение установлено, False - потеряно
    config_confirmed = pyqtSignal(dict)   # (не используется, но зарезервировано)

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.1):
        super().__init__()
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial = None
        self._running = False   # флаг работы потока
        self._connected = False # флаг активности соединения

    def run(self):
        """Точка входа потока: устанавливает соединение и запускает цикл чтения."""
        self._running = True
        self._connect()

        while self._running:
            if self._connected:
                self._read_data()
            self.msleep(10)  # небольшая пауза, чтобы не нагружать процессор

    def _connect(self):
        """Открытие порта и сброс буферов."""
        try:
            self.serial = serial.Serial(
                port=self.port_name,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=1.0
            )
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
            self._connected = True
            self.connection_changed.emit(True)
            self.status_message.emit(f"Подключено к {self.port_name} @ {self.baudrate}", "INFO")

            # Даём Arduino время на инициализацию (загрузка конфигурации из EEPROM)
            self.msleep(600)

        except serial.SerialException as e:
            self._connected = False
            self.status_message.emit(f"Ошибка подключения: {e}", "ERROR")
            self.connection_changed.emit(False)

    def _read_data(self):
        """Чтение доступных строк из порта, парсинг JSON и генерация сигналов."""
        try:
            while self.serial.in_waiting > 0:
                line = self.serial.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)

                    # Основная телеметрия: обязательно содержит ключи 'a', 'e', 's', 'r'
                    if all(k in data for k in ['a', 'e', 's', 'r']):
                        self.telemetry_received.emit(data)

                    # Сервисные сообщения от Arduino (статус, подтверждения)
                    elif 'status' in data:
                        if data['status'] == 'init':
                            self.status_message.emit(
                                f"МК инициализирован. Порог: {data.get('threshold', 'N/A')}", "INFO"
                            )
                        elif data['status'] == 'config_saved':
                            self.status_message.emit("Конфигурация сохранена в EEPROM", "SUCCESS")
                        elif data['status'] == 'manual_updated':
                            relay = "ВКЛ" if data.get('r') else "ВЫКЛ"
                            self.status_message.emit(f"Ручное управление: реле {relay}", "ACTION")

                except json.JSONDecodeError:
                    continue  # игнорируем некорректные строки

        except serial.SerialException as e:
            # Ошибка чтения обычно означает разрыв соединения
            self._connected = False
            self.status_message.emit(f"Потеря связи: {e}", "ERROR")
            self.connection_changed.emit(False)

    def send_command(self, command: dict):
        """Отправка произвольной JSON-команды в Arduino (добавляет перевод строки)."""
        if not self._connected or not self.serial:
            self.status_message.emit("Нет соединения с МК", "ERROR")
            return

        try:
            payload = json.dumps(command, separators=(',', ':')) + '\n'
            self.serial.write(payload.encode('utf-8'))
            self.serial.flush()
        except serial.SerialException as e:
            self.status_message.emit(f"Ошибка отправки: {e}", "ERROR")

    def send_config(self, config: dict):
        """
        Формирует команду конфигурации из словаря и отправляет.
        Ожидаемые ключи: threshold, hysteresis, N, alpha, t_min, t_max, cooldown.
        """
        command = {
            "cmd": "config",
            "T": int(config.get('threshold', 300)),
            "delta": int(config.get('hysteresis', 30)),
            "N": int(config.get('N', 8)),
            "alpha": float(config.get('alpha', 0.15)),
            "t_min": int(config.get('t_min', 20)),
            "t_max": int(config.get('t_max', 150)),
            "cooldown": int(config.get('cooldown', 400))
        }
        self.send_command(command)

    def send_manual(self, action: str):
        """Отправка команды ручного управления: on, off, toggle."""
        if action not in ('on', 'off', 'toggle'):
            return
        self.send_command({"cmd": "manual", "action": action})

    def disconnect(self):
        """Остановка потока и закрытие порта."""
        self._running = False
        if self.serial and self.serial.is_open:
            self.serial.close()
        self.wait(1000)  # ожидаем завершения потока

    @staticmethod
    def list_ports() -> list[str]:
        """Возвращает список доступных COM-портов в формате 'порт (описание)'."""
        ports = serial.tools.list_ports.comports()
        return [f"{p.device} ({p.description})" for p in ports]


class SoundLightApp(QMainWindow):
    """Главное окно приложения: графики, настройки, логи, ручное управление."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Умное освещение: Звуковое управление")
        self.resize(1150, 780)

        self.current_port = None          # текущий выбранный порт
        self.serial_worker = None         # экземпляр потока SerialWorker

        # Буферы для отображения графиков (кольцевые очереди фиксированной длины)
        self.max_points = 300
        self.adc_data = deque([512] * self.max_points, maxlen=self.max_points)  # сырой ADC
        self.env_data = deque([0.0] * self.max_points, maxlen=self.max_points)  # огибающая

        self.light_state = False           # текущее состояние света (вкл/выкл)
        # Актуальные параметры конфигурации (используются при отправке)
        self.current_config = {
            'threshold': 300, 'hysteresis': 30, 'N': 8, 'alpha': 0.15,
            't_min': 20, 't_max': 150, 'cooldown': 400
        }

        self._apply_dark_style()   # установка тёмной темы через QSS
        self.init_ui()

    def _apply_dark_style(self):
        """Стилизация всех виджетов с помощью QSS (тёмная тема, акцент на синий)."""
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e1e; }
            QTabWidget::pane { border: 1px solid #444; background: #2d2d2d; border-radius: 8px; padding: 5px; }
            QTabBar::tab { background: #3c3c3c; color: #aaa; border: 1px solid #555; border-bottom: none; 
                           border-top-left-radius: 6px; border-top-right-radius: 6px; padding: 8px 16px; margin-right: 2px; }
            QTabBar::tab:hover { background: #4a4a4a; }
            QTabBar::tab:selected { background: #3498db; color: white; border-color: #3498db; }
            QGroupBox { font-weight: 600; border: 1px solid #555; border-radius: 6px; 
                        margin-top: 12px; padding-top: 10px; background: #2a2a2a; color: #ecf0f1; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #3498db; }
            QPushButton { background-color: #3498db; color: white; border: none; border-radius: 5px; padding: 8px 16px; font-weight: 500; }
            QPushButton:hover { background-color: #2980b9; }
            QPushButton:pressed { background-color: #1f618d; }
            QPushButton:disabled { background-color: #555; color: #888; }
            QPushButton#btn_connect { background-color: #27ae60; }
            QPushButton#btn_connect:hover { background-color: #219653; }
            QPushButton#btn_disconnect { background-color: #e74c3c; }
            QPushButton#btn_disconnect:hover { background-color: #c0392b; }
            QProgressBar { border: 1px solid #555; border-radius: 4px; text-align: center; height: 18px; background: #333; color: #eee; }
            QProgressBar::chunk { background: #2ecc71; border-radius: 3px; }
            QComboBox { border: 1px solid #555; border-radius: 4px; padding: 5px; background: #333; color: #eee; }
            QComboBox::drop-down { background: #444; border-left: 1px solid #555; border-top-right-radius: 4px; border-bottom-right-radius: 4px; }
            QPlainTextEdit { background: #1a1a1a; color: #d4d4d4; font-family: 'Consolas', 'Courier New', monospace; 
                             border: 1px solid #444; border-radius: 6px; font-size: 13px; }
            QLabel { color: #ecf0f1; }
            QLabel.status_on { color: #f1c40f; font-weight: bold; font-size: 16px; }
            QLabel.status_off { color: #95a5a6; font-size: 16px; }
            QSlider::groove:horizontal { background: #444; height: 6px; border-radius: 3px; }
            QSlider::handle:horizontal { background: #3498db; width: 18px; border-radius: 9px; margin: -6px 0; }
            QSpinBox, QDoubleSpinBox { background: #333; color: #eee; border: 1px solid #555; border-radius: 4px; padding: 3px; }
        """)

    def init_ui(self):
        """Построение интерфейса: верхняя панель, вкладки."""
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # --- Верхняя панель с выбором порта и подключением ---
        top_bar = QHBoxLayout()
        self.com_box = QComboBox()
        self.com_box.setEditable(True)
        self.com_box.setPlaceholderText("Выберите порт...")
        self._refresh_ports()

        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedWidth(30)
        refresh_btn.clicked.connect(self._refresh_ports)

        self.btn_connect = QPushButton("Подключиться", objectName="btn_connect")
        self.btn_connect.clicked.connect(self.toggle_connection)

        self.lbl_status_conn = QLabel("🔴 Не подключено")
        self.lbl_status_conn.setStyleSheet("color: #e74c3c; font-weight: bold;")

        top_bar.addWidget(QLabel("Порт:"))
        top_bar.addWidget(self.com_box)
        top_bar.addWidget(refresh_btn)
        top_bar.addSpacing(20)
        top_bar.addWidget(self.btn_connect)
        top_bar.addStretch()
        top_bar.addWidget(self.lbl_status_conn)
        layout.addLayout(top_bar)

        # --- Вкладки ---
        self.tabs = QTabWidget()
        self.tabs.addTab(self._create_monitor_tab(), "🖥 Мониторинг")
        self.tabs.addTab(self._create_settings_tab(), "⚙️ Настройки")
        self.tabs.addTab(self._create_logs_tab(), "📋 Журнал событий")
        layout.addWidget(self.tabs)

    def _refresh_ports(self):
        """Обновить список COM-портов, сохранив текущий выбор, если он ещё доступен."""
        current = self.com_box.currentText().split(' ')[0]
        self.com_box.clear()
        ports = SerialWorker.list_ports()
        if not ports:
            self.com_box.addItem("Нет доступных портов")
        else:
            self.com_box.addItems(ports)
            for i, p in enumerate(ports):
                if p.startswith(current):
                    self.com_box.setCurrentIndex(i)
                    break

    def _create_monitor_tab(self) -> QWidget:
        """Вкладка мониторинга: индикатор света, ручное управление, прогресс-бар шума, график."""
        tab = QWidget()
        layout = QHBoxLayout(tab)

        # Левая панель: состояние, ручное управление, уровень шума
        left = QVBoxLayout()

        # --- Индикатор освещения ---
        light_box = QGroupBox("Состояние освещения")
        light_layout = QVBoxLayout()
        self.lbl_light_icon = QLabel("🔌", alignment=Qt.AlignmentFlag.AlignCenter)
        self.lbl_light_icon.setStyleSheet("font-size: 48px;")
        self.lbl_light_state = QLabel("Выключено", alignment=Qt.AlignmentFlag.AlignCenter)
        self.lbl_light_state.setStyleSheet("color: #95a5a6; font-size: 16px; font-weight: bold;")
        light_layout.addWidget(self.lbl_light_icon)
        light_layout.addWidget(self.lbl_light_state)
        light_box.setLayout(light_layout)
        left.addWidget(light_box)

        # --- Ручное управление реле ---
        ctrl_box = QGroupBox("Ручное управление")
        ctrl_layout = QHBoxLayout()
        self.btn_on = QPushButton("ВКЛ")
        self.btn_on.setStyleSheet("background-color: #f39c12;")
        self.btn_off = QPushButton("ВЫКЛ")
        self.btn_off.setStyleSheet("background-color: #7f8c8d;")
        self.btn_toggle = QPushButton("ПЕРЕКЛ.")
        self.btn_toggle.setStyleSheet("background-color: #8e44ad;")

        self.btn_on.clicked.connect(lambda: self._send_manual("on"))
        self.btn_off.clicked.connect(lambda: self._send_manual("off"))
        self.btn_toggle.clicked.connect(lambda: self._send_manual("toggle"))

        ctrl_layout.addWidget(self.btn_on)
        ctrl_layout.addWidget(self.btn_off)
        ctrl_layout.addWidget(self.btn_toggle)
        ctrl_box.setLayout(ctrl_layout)
        left.addWidget(ctrl_box)

        # --- Уровень шума (сырой ADC) ---
        vol_box = QGroupBox("Текущий уровень шума")
        vol_layout = QVBoxLayout()
        self.vol_bar = QProgressBar()
        self.vol_bar.setRange(0, 1023)
        self.vol_bar.setValue(0)
        self.lbl_vol_text = QLabel("0 / 1023")
        self.lbl_vol_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vol_layout.addWidget(self.vol_bar)
        vol_layout.addWidget(self.lbl_vol_text)
        vol_box.setLayout(vol_layout)
        left.addWidget(vol_box)
        left.addStretch()
        layout.addLayout(left, 1)

        # Правая панель: графики (сигнал + огибающая)
        right = QVBoxLayout()
        graph_box = QGroupBox("Форма звукового сигнала")
        graph_layout = QVBoxLayout()

        self.plot = pg.PlotWidget()
        self.plot.setBackground('#1e1e1e')
        self.plot.setTitle("ADC (сырой) и Огибающая (Envelope)", color="#ecf0f1", size="12pt")
        self.plot.setLabel('left', 'Значение', color='#aaa')
        self.plot.setLabel('bottom', 'Время (мс)', color='#aaa')
        self.plot.getAxis('left').setPen(pg.mkPen('#666'))
        self.plot.getAxis('bottom').setPen(pg.mkPen('#666'))
        self.plot.getAxis('left').setTextPen(pg.mkPen('#aaa'))
        self.plot.getAxis('bottom').setTextPen(pg.mkPen('#aaa'))
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.addLegend()

        self.curve_adc = self.plot.plot(pen=pg.mkPen(color='#3498db', width=2), name='Сигнал')
        self.curve_env = self.plot.plot(pen=pg.mkPen(color='#e74c3c', width=3), name='Огибающая')

        graph_layout.addWidget(self.plot)
        graph_box.setLayout(graph_layout)
        right.addWidget(graph_box)
        layout.addLayout(right, 2)

        return tab

    def _create_settings_tab(self) -> QWidget:
        """Вкладка настроек: порог, защитный интервал, параметры фильтрации, профили."""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Основные параметры: порог срабатывания и задержка между срабатываниями
        basic_box = QGroupBox("Основные параметры")
        basic_layout = QFormLayout()

        self.sld_thresh = QSlider(Qt.Orientation.Horizontal)
        self.sld_thresh.setRange(50, 800)
        self.sld_thresh.setValue(300)
        self.lbl_thresh = QLabel("300")
        self.sld_thresh.valueChanged.connect(lambda v: self.lbl_thresh.setText(str(v)))
        basic_layout.addRow("Чувствительность (порог):", self.sld_thresh)
        basic_layout.addRow("", self.lbl_thresh)

        self.sld_cooldown = QSlider(Qt.Orientation.Horizontal)
        self.sld_cooldown.setRange(100, 2000)
        self.sld_cooldown.setValue(400)
        self.lbl_cooldown = QLabel("400 мс")
        self.sld_cooldown.valueChanged.connect(lambda v: self.lbl_cooldown.setText(f"{v} мс"))
        basic_layout.addRow("Защитный интервал:", self.sld_cooldown)
        basic_layout.addRow("", self.lbl_cooldown)
        basic_box.setLayout(basic_layout)
        layout.addWidget(basic_box)

        # Расширенные настройки: размер окна, альфа-фильтр, мин/макс длительность
        adv_box = QGroupBox("Расширенные настройки фильтрации")
        adv_layout = QFormLayout()

        self.spb_n = QSpinBox()
        self.spb_n.setRange(2, 16)
        self.spb_n.setValue(8)
        adv_layout.addRow("Размер окна усреднения (N):", self.spb_n)

        self.spb_alpha = QDoubleSpinBox()
        self.spb_alpha.setRange(0.01, 0.5)
        self.spb_alpha.setSingleStep(0.01)
        self.spb_alpha.setValue(0.15)
        adv_layout.addRow("Сглаживание огибающей (α):", self.spb_alpha)

        self.spb_tmin = QSpinBox()
        self.spb_tmin.setRange(5, 100)
        self.spb_tmin.setValue(20)
        adv_layout.addRow("Мин. длительность хлопка:", self.spb_tmin)

        self.spb_tmax = QSpinBox()
        self.spb_tmax.setRange(100, 500)
        self.spb_tmax.setValue(150)
        adv_layout.addRow("Макс. длительность шума:", self.spb_tmax)

        adv_box.setLayout(adv_layout)
        layout.addWidget(adv_box)

        # Профили конфигурации (быстрая загрузка предустановок, но без автоматической отправки)
        prof_box = QGroupBox("Профили конфигурации")
        prof_layout = QHBoxLayout()
        self.combo_profile = QComboBox()
        self.combo_profile.addItems(["🌞 Дневной", "🌙 Ночной", "🎤 Презентация", "🔧 Пользовательский"])
        btn_save = QPushButton("Сохранить в МК")
        btn_save.clicked.connect(self._save_config)
        prof_layout.addWidget(self.combo_profile)
        prof_layout.addWidget(btn_save)
        prof_box.setLayout(prof_layout)
        layout.addWidget(prof_box)

        layout.addStretch()
        return tab

    def _create_logs_tab(self) -> QWidget:
        """Вкладка с текстовым логом всех событий."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.appendPlainText("[СИСТЕМА] Приложение запущено. Ожидание подключения к Arduino...")
        layout.addWidget(self.log_edit)
        return tab

    def _log(self, msg: str, level: str = "INFO"):
        """Добавить сообщение в журнал событий с временной меткой."""
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{ts}] [{level}]"
        self.log_edit.appendPlainText(f"{prefix} {msg}")
        # Автоматическая прокрутка вниз
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def toggle_connection(self):
        """Подключение / отключение от последовательного порта."""
        if self.serial_worker and self.serial_worker.isRunning():
            # Отключение
            self._log("Завершение соединения...", "INFO")
            self.serial_worker.disconnect()
            self.serial_worker = None
            self.btn_connect.setText("Подключиться")
            self.btn_connect.setObjectName("btn_connect")
            self.lbl_status_conn.setText("🔴 Не подключено")
            self.lbl_status_conn.setStyleSheet("color: #e74c3c; font-weight: bold;")
            self._set_controls_enabled(False)
        else:
            # Подключение
            port_text = self.com_box.currentText()
            port = port_text.split(' ')[0]
            self._log(f"Подключение к {port}...", "INFO")

            self.serial_worker = SerialWorker(port)
            # Привязываем сигналы к слотам
            self.serial_worker.telemetry_received.connect(self._on_telemetry)
            self.serial_worker.status_message.connect(self._log)
            self.serial_worker.connection_changed.connect(self._on_connection_changed)
            self.serial_worker.start()

            self.btn_connect.setText("Отключиться")
            self.btn_connect.setObjectName("btn_disconnect")
            self._set_controls_enabled(True)

    def _on_connection_changed(self, connected: bool):
        """Слот для сигнала connection_changed: обновить индикацию и логи."""
        if connected:
            self.lbl_status_conn.setText("🟢 Подключено (115200 бод)")
            self.lbl_status_conn.setStyleSheet("color: #2ecc71; font-weight: bold;")
            self._log("Соединение установлено. Ожидание инициализации МК...", "INFO")
        else:
            self.lbl_status_conn.setText("🔴 Не подключено")
            self.lbl_status_conn.setStyleSheet("color: #e74c3c; font-weight: bold;")
            self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled: bool):
        """Включить/выключить элементы управления, зависящие от соединения."""
        self.btn_on.setEnabled(enabled)
        self.btn_off.setEnabled(enabled)
        self.btn_toggle.setEnabled(enabled)
        self.sld_thresh.setEnabled(enabled)
        self.sld_cooldown.setEnabled(enabled)
        self.spb_n.setEnabled(enabled)
        self.spb_alpha.setEnabled(enabled)
        self.spb_tmin.setEnabled(enabled)
        self.spb_tmax.setEnabled(enabled)

    def _send_manual(self, action: str):
        """Отправить команду ручного управления через SerialWorker."""
        if not self.serial_worker:
            self._log("Нет соединения с МК", "ERROR")
            return
        self.serial_worker.send_manual(action)
        self._log(f"Команда ручного управления: {action.upper()}", "COMMAND")

    def _save_config(self):
        """Собрать текущие значения с виджетов и отправить их в Arduino."""
        if not self.serial_worker:
            self._log("Нет соединения с МК", "ERROR")
            return

        self.current_config = {
            'threshold': self.sld_thresh.value(),
            'hysteresis': 30,            # фиксированная гистерезисная зона (не вынесена в UI)
            'N': self.spb_n.value(),
            'alpha': self.spb_alpha.value(),
            't_min': self.spb_tmin.value(),
            't_max': self.spb_tmax.value(),
            'cooldown': self.sld_cooldown.value()
        }

        profile = self.combo_profile.currentText()
        self._log(f"Сохранение профиля '{profile}' в МК...", "INFO")
        self.serial_worker.send_config(self.current_config)

    def _on_telemetry(self, data: dict):
        """
        Обработка входящей телеметрии.
        Обновляет графики, прогресс-бар, индикатор освещения.
        """
        adc = data.get('a', 512)
        env = data.get('e', 0.0)

        # Добавляем новые точки в буферы
        self.adc_data.append(adc)
        self.env_data.append(env)

        # Перерисовка графиков (передаём текущие списки)
        self.curve_adc.setData(list(self.adc_data))
        self.curve_env.setData(list(self.env_data))

        # Обновление прогресс-бара уровня шума
        self.vol_bar.setValue(adc)
        self.lbl_vol_text.setText(f"{adc} / 1023")

        # Обновление состояния реле, если оно изменилось
        relay = data.get('r', 0)
        if relay != self.light_state:
            self.light_state = bool(relay)
            self._update_light_indicator()

    def _update_light_indicator(self):
        """Изменить иконку и текст индикатора освещения в соответствии с self.light_state."""
        if self.light_state:
            self.lbl_light_state.setText("Включено")
            self.lbl_light_state.setStyleSheet("color: #f1c40f; font-weight: bold;")
            self.lbl_light_icon.setText("💡")
        else:
            self.lbl_light_state.setText("Выключено")
            self.lbl_light_state.setStyleSheet("color: #95a5a6; font-size: 16px; font-weight: bold;")
            self.lbl_light_icon.setText("🔌")

    def closeEvent(self, event):
        """При закрытии окна корректно завершаем поток и закрываем порт."""
        self._log("Завершение работы приложения...", "INFO")
        if self.serial_worker and self.serial_worker.isRunning():
            self.serial_worker.disconnect()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # современный стиль, хорошо сочетается с тёмной темой

    # При желании можно установить иконку приложения: app.setWindowIcon(QIcon("icon.png"))

    window = SoundLightApp()
    window.show()

    sys.exit(app.exec())