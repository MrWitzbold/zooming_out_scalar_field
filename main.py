import random

import numpy as np
import matplotlib.cm as cm
import pystencils.plot as plt
from pystencils.session import *
import math

def example_scalar_field(t=0):
    x, y = np.meshgrid(np.linspace(-3., 3., 1000), np.linspace(-3., 3., 1000))
    z = (np.sin(x * t/50) + np.cos(y * t/50))
    return z

t = 0
def run_func():
    global t
    t += 1
    return example_scalar_field(t)

animation = plt.scalar_field_animation(run_func, frames=200)
plt.show()
