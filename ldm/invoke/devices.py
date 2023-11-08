import torch
from torch import autocast
from contextlib import nullcontext

def choose_torch_device() -> str:
    '''Convenience routine for guessing which GPU device to run model on'''
    if torch.cuda.is_available():
        return 'cuda'
    if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'

def choose_precision(device) -> str:
    '''Returns an appropriate precision for the given torch device'''
    if device.type == 'cuda':
        device_name = torch.cuda.get_device_name(device)
        if (
            'GeForce GTX 1660' not in device_name
            and 'GeForce GTX 1650' not in device_name
        ):
            return 'float16'
    return 'float32'

def choose_autocast(precision):
    '''Returns an autocast context or nullcontext for the given precision string'''
    # float16 currently requires autocast to avoid errors like:
    # 'expected scalar type Half but found Float'
    return autocast if precision in ['autocast', 'float16'] else nullcontext
