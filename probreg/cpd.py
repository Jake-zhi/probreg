from __future__ import print_function
from __future__ import division
import abc
from collections import namedtuple
import six
import numpy as np
import open3d as o3
from . import gauss_transform as gt
from . import math_utils as mu

EstepResult = namedtuple('EstepResult', ['pt1', 'p1', 'px', 'n_p'])
RigidResult = namedtuple('RigidResult', ['rot', 't', 'scale', 'sigma2', 'q'])
AffineResult = namedtuple('AffineResult', ['affine', 't', 'sigma2', 'q'])
NonRigidResult = namedtuple('NonRigidResult', ['kernel', 'coeff', 'sigma2', 'q'])


@six.add_metaclass(abc.ABCMeta)
class CoherentPointDrift():
    def __init__(self):
        self._gt = gt
        self._result_type = None

    @classmethod
    def transform(cls, points, params,
                  array_type=o3.Vector3dVector):
        if isinstance(points, array_type):
            return array_type(cls._transform(np.asarray(points), params))
        return cls._transform(points, params)

    @staticmethod
    @abc.abstractmethod
    def _transform(points, params):
        return points

    def expectation_step(self, t_source, target, sigma2, w=0.0):
        """
        Expectation step
        """
        assert t_source.ndim == 2 and target.ndim == 2, "source and target must have 2 dimensions."
        ndim = t_source.shape[1]
        h = np.sqrt(2.0 * sigma2)
        c = (2.0 * np.pi * sigma2) ** (ndim * 0.5)
        c *= w / (1.0 - w) * t_source.shape[0] / target.shape[0]
        gtrans = self._gt.GaussTransform(t_source, h)
        kt1 = gtrans.compute(target)
        kt1[kt1==0] = np.finfo(float).eps
        a = 1.0 / (kt1 + c)
        pt1 = 1.0 - c * a
        gtrans = self._gt.GaussTransform(target, h)
        p1 = gtrans.compute(t_source, a)
        px = gtrans.compute(t_source, np.tile(a, (ndim, 1)) * target.T).T
        return EstepResult(pt1, p1, px, np.sum(p1))

    @abc.abstractclassmethod
    def maximization_step(self, source, target, estep_res):
        return None

    def registration(self, source, target,
                     w=0.0, max_iteration=50,
                     tolerance=0.001):
        assert not self._result_type is None, "result type of computing registration is None."
        ndim = source.shape[1]
        sigma2 = mu.mean_square_norm(source, target)
        q = -tolerance + 1.0 - target.shape[0] * ndim * 0.5 * np.log(sigma2)
        res = self._result_type(np.identity(ndim), np.zeros(3), 1.0, sigma2, q)
        for _ in range(max_iteration):
            t_source = self.transform(source, res)
            estep_res = self.expectation_step(t_source, target, res.sigma2, w)
            res = self.maximization_step(source, target, estep_res)
            if abs(res.q - q) < tolerance:
                break
            q = res.q
        return res

class RigidCPD(CoherentPointDrift):
    def __init__(self):
        super(RigidCPD, self).__init__()
        self._result_type = RigidResult

    @staticmethod
    def _transform(points, params):
        rot, t, scale, _, _ = params
        return scale * np.dot(points, rot.T) + t

    def maximization_step(self, source, target, estep_res):
        pt1, p1, px, n_p = estep_res
        ndim = source.shape[1]
        mu_x = np.sum(px, axis=0) / n_p
        mu_y = np.dot(source.T, p1) / n_p
        target_hat = target - mu_x
        source_hat = source - mu_y
        a = np.dot(px.T, source_hat) - np.outer(mu_x, np.dot(p1.T, source_hat))
        u, _, vh = np.linalg.svd(a, full_matrices=True)
        c = np.ones(ndim)
        c[-1] = np.linalg.det(np.dot(u, vh))
        rot = np.dot(u * c, vh)
        tr_atr = np.trace(np.dot(a.T, rot))
        tr_yp1y = np.trace(np.dot(source_hat.T * p1, source_hat))
        scale = tr_atr / tr_yp1y
        t = mu_x - scale * np.dot(rot, mu_y)
        tr_xp1x = np.trace(np.dot(target_hat.T * pt1, target_hat))
        sigma2 = (tr_xp1x - scale * tr_atr) / (n_p * ndim)
        q = (tr_xp1x - 2.0 * scale * tr_atr + (scale ** 2) * tr_yp1y) / (2.0 * sigma2) + ndim * n_p * 0.5 * np.log(sigma2)
        return RigidResult(rot, t, scale, sigma2, q)


class AffineCPD(CoherentPointDrift):
    def __init__(self):
        super(AffineCPD, self).__init__()
        self._result_type = AffineResult

    @staticmethod
    def _transform(points, params):
        affine, t, _, _ = params
        return np.dot(points, affine.T) + t

    def maximization_step(self, source, target, estep_res):
        pt1, p1, px, n_p = estep_res
        ndim = source.shape[1]
        mu_x = np.sum(px, axis=0) / n_p
        mu_y = np.dot(source.T, p1) / n_p
        target_hat = target - mu_x
        source_hat = source - mu_y
        a = np.dot(px.T, source_hat) - np.outer(mu_x, np.dot(p1.T, source_hat))
        yp1y = np.dot(source_hat.T * p1, source_hat)
        affine = np.linalg.solve(yp1y.T, a.T).T
        t = mu_x - np.dot(affine, mu_y)
        tr_xp1x = np.trace(np.dot(target_hat.T * pt1, target_hat))
        tr_xpyb = np.trace(np.dot(a, affine.T))
        sigma2 = (tr_xp1x - tr_xpyb) / (n_p * ndim)
        tr_ab = np.trace(np.dot(a, affine.T))
        q = (tr_xp1x - 2 * tr_ab + tr_xpyb) / (2.0 * sigma2) + ndim * n_p * 0.5 * np.log(sigma2)
        return AffineResult(affine, t, sigma2, q)


class NonRigidCPD(CoherentPointDrift):
    def __init__(self):
        super(NonRigidCPD, self).__init__()
        self._result_type = NonRigidResult

    @staticmethod
    def _transform(points, params):
        pass

    def maximization_step(self, source, target, estep_res):
        pass


def registration_cpd(source, target, transform_type='rigid',
                     w=0.0, max_iteration=100, tolerance=0.001):
    if transform_type == 'rigid':
        cpd = RigidCPD()
    elif transform_type == 'affine':
        cpd = AffineCPD()
    elif transform_type == 'nonrigid':
        cpd = NonRigidCPD()
    else:
        raise ValueError('Unknown transform_type %s' % transform_type)
    return cpd.registration(np.asarray(source.points), np.asarray(target.points),
                            w, max_iteration, tolerance)