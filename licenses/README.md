# Компоненты и исходники

Библиотеки поставляются отдельными файлами, без изменения их кода. Их лицензии не заменяются лицензией клиента.

| Компонент | Версия / расположение лицензии | Исходники |
|---|---|---|
| CPython, стандартная библиотека | 3.9.13, `Python.txt`; Tcl/Tk — также `../runtime/tcl` | https://www.python.org/downloads/release/python-3913/ |
| PySide6 / shiboken6 | 6.10.3; LGPLv3/GPL, тексты в этой папке и `python-packages` | https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v6.10.3 |
| Qt / QtWebEngine / Chromium | Qt 6.10.3, Chromium 134.0.6998.208; `Qt-6.10.3` | https://download.qt.io/official_releases/qt/6.10/6.10.3/submodules/ |
| CustomTkinter | 5.2.2, MIT, `python-packages` | https://github.com/TomSchimansky/CustomTkinter/tree/v5.2.2 |
| darkdetect | 0.8.0, BSD, `python-packages` | https://github.com/albertosottile/darkdetect/tree/v0.8.0 |
| packaging | 26.3, Apache/BSD, `python-packages` | https://github.com/pypa/packaging |
| websockets | 15.0.1, BSD, `python-packages` | https://github.com/python-websockets/websockets/tree/15.0.1 |
| NeoScrypt | BSD, `NeoScrypt.txt`; помощник с параметризованной длиной | `../native-src`; https://github.com/ghostlander/NeoScrypt/tree/89c04c75a674974936c6579b42764d5370139d3d |

`Qt-6.10.3` содержит локальные копии уведомлений из документации Qt 6.10, включая Chromium и сторонние компоненты. Ссылки на первоисточники сохранены внутри файлов. Полный индекс может описывать и компоненты, не включённые в этот минимальный runtime.

Для LGPL-компонентов допускается замена совместимыми сборками и отладка изменений согласно их лицензиям; библиотечные DLL не запакованы в закрытый EXE. Само наличие этого каталога не является юридической проверкой всех условий дальнейшего публичного распространения. Перед публикацией обеспечь требуемый применимыми лицензиями доступ к соответствующим исходникам и уведомлениям.

Тексты GNU-лицензий получены из каталога SPDX: https://github.com/spdx/license-list-data/tree/main/text . Условия Qt: https://doc.qt.io/qt-6.10/qtwebengine-licensing.html .
