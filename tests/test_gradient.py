import numpy as np
import pytest
from numpy import linalg

from examples.seismic.acoustic.acoustic_example import setup, smooth10
from examples.seismic import Receiver


@pytest.mark.parametrize('space_order', [4])
@pytest.mark.parametrize('time_order', [2])
@pytest.mark.parametrize('dimensions', [(70, 80)])
def test_gradientFWI(dimensions, time_order, space_order):
    """
    This test ensure that the FWI gradient computed with devito
    satisfies the Taylor expansion property:
    .. math::
        \Phi(m0 + h dm) = \Phi(m0) + \O(h) \\
        \Phi(m0 + h dm) = \Phi(m0) + h \nabla \Phi(m0) + \O(h^2) \\
        \Phi(m0) = .5* || F(m0 + h dm) - D ||_2^2

    where
    .. math::
        \nabla \Phi(m0) = <J^T \delta d, dm> \\
        \delta d = F(m0+ h dm) - D \\

    with F the Forward modelling operator.
    :param dimensions: size of the domain in all dimensions
    in number of grid points
    :param time_order: order of the time discretization scheme
    :param space_order: order of the spacial discretization scheme
    :return: assertion that the Taylor properties are satisfied
    """
    wave = setup(dimensions=dimensions, time_order=time_order,
                 space_order=space_order, nbpml=40, tn=500.,
                 dse='noop', dle='noop')
    m0 = smooth10(wave.model.m.data, wave.model.shape_domain)
    dm = np.float32(wave.model.m.data - m0)

    # Compute receiver data for the true velocity
    rec, u, _ = wave.forward()
    # Compute receiver data and full wavefield for the smooth velocity
    rec0, u0, _ = wave.forward(m=m0, save=True)

    # Objective function value
    F0 = .5*linalg.norm(rec0.data - rec.data)**2
    # Gradient: <J^T \delta d, dm>
    residual = Receiver(name='rec', data=rec0.data - rec.data,
                        coordinates=rec0.coordinates.data)
    gradient, _ = wave.gradient(residual, u0, m=m0)
    G = np.dot(gradient.data.reshape(-1), dm.reshape(-1))

    # FWI Gradient test
    H = [0.5, 0.25, .125, 0.0625, 0.0312, 0.015625, 0.0078125]
    error1 = np.zeros(7)
    error2 = np.zeros(7)
    for i in range(0, 7):
        # Add the perturbation to the model
        mloc = m0 + H[i] * dm
        # Data for the new model
        d = wave.forward(m=mloc)[0]
        # First order error Phi(m0+dm) - Phi(m0)
        error1[i] = np.absolute(.5*linalg.norm(d.data - rec.data)**2 - F0)
        # Second order term r Phi(m0+dm) - Phi(m0) - <J(m0)^T \delta d, dm>
        error2[i] = np.absolute(.5*linalg.norm(d.data - rec.data)**2 - F0 - H[i] * G)
        # print(F0, .5*linalg.norm(d - rec)**2, error1[i], H[i] *G, error2[i])
        # print('For h = ', H[i], '\nFirst order errors is : ', error1[i],
        #       '\nSecond order errors is ', error2[i])

    hh = np.zeros(7)
    for i in range(0, 7):
        hh[i] = H[i] * H[i]

    # Test slope of the  tests
    p1 = np.polyfit(np.log10(H), np.log10(error1), 1)
    p2 = np.polyfit(np.log10(H), np.log10(error2), 1)
    print(p1)
    print(p2)
    assert np.isclose(p1[0], 1.0, rtol=0.1)
    assert np.isclose(p2[0], 2.0, rtol=0.1)


@pytest.mark.parametrize('space_order', [4])
@pytest.mark.parametrize('time_order', [2])
@pytest.mark.parametrize('dimensions', [(70, 80)])
def test_gradientJ(dimensions, time_order, space_order):
    """
    This test ensure that the Jacobian computed with devito
    satisfies the Taylor expansion property:
    .. math::
        F(m0 + h dm) = F(m0) + \O(h) \\
        F(m0 + h dm) = F(m0) + J dm + \O(h^2) \\

    with F the Forward modelling operator.
    :param dimensions: size of the domain in all dimensions
    in number of grid points
    :param time_order: order of the time discretization scheme
    :param space_order: order of the spacial discretization scheme
    :return: assertion that the Taylor properties are satisfied
    """
    wave = setup(dimensions=dimensions, time_order=time_order,
                 space_order=space_order, tn=750.,
                 nbpml=100)
    m0 = smooth10(wave.model.m.data, wave.model.shape_domain)
    dm = np.float32(wave.model.m.data - m0)
    # Compute receiver data and full wavefield for the smooth velocity
    rec, u0, _ = wave.forward(m=m0, save=False)
    # Gradient: J dm
    Jdm, _, _, _ = wave.born(dm, m=m0)
    # FWI Gradient test
    H = [0.5, 0.25, .125, 0.0625, 0.0312, 0.015625, 0.0078125]
    error1 = np.zeros(7)
    error2 = np.zeros(7)
    for i in range(0, 7):
        # Add the perturbation to the model
        mloc = m0 + H[i] * dm
        # Data for the new model
        d = wave.forward(m=mloc)[0]
        # First order error F(m0 + hdm) - F(m0)
        error1[i] = np.linalg.norm(d.data - rec.data, 1)
        # Second order term F(m0 + hdm) - F(m0) - J dm
        error2[i] = np.linalg.norm(d.data - rec.data - H[i] * Jdm.data, 1)
        # print(F0, .5*linalg.norm(d - rec)**2, error1[i], H[i] *G, error2[i])
        # print('For h = ', H[i], '\nFirst order errors is : ', error1[i],
        #       '\nSecond order errors is ', error2[i])

    hh = np.zeros(7)
    for i in range(0, 7):
        hh[i] = H[i] * H[i]

    # Test slope of the  tests
    p1 = np.polyfit(np.log10(H), np.log10(error1), 1)
    p2 = np.polyfit(np.log10(H), np.log10(error2), 1)
    print(p1)
    print(p2)
    assert np.isclose(p1[0], 1.0, rtol=0.1)
    assert np.isclose(p2[0], 2.0, rtol=0.1)


if __name__ == "__main__":
    test_gradientJ(dimensions=(60, 70), time_order=2, space_order=4)