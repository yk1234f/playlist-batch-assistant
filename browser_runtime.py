"""Use installed browsers only; never download a browser implicitly."""
BROWSER_OPTIONS = {'自动（Edge → Chrome）': 'auto', 'Microsoft Edge': 'msedge', 'Google Chrome': 'chrome'}


def launch_choices(channel=None):
    channel = channel or 'auto'
    choices = [('Microsoft Edge', {'channel': 'msedge'}), ('Google Chrome', {'channel': 'chrome'})]
    if channel == 'auto':
        return choices
    if channel in ('chrome', 'msedge'):
        return [item for item in choices if item[1]['channel'] == channel]
    raise ValueError('不支持的浏览器选项，请选择自动、Edge 或 Chrome')
