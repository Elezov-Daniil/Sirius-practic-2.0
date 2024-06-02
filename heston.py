from typing import Union
from numpy import float_
from numpy.typing import NDArray
from numba.types import CPointer, float64, complex128, intc  # type: ignore
from dataclasses import dataclass
import math
import cmath
import numpy as np
import numba                     # type: ignore
import scipy.stats as st         # type: ignore
import scipy.optimize as opt     # type: ignore
import scipy.integrate as intg   # type: ignore
import scipy.special as spec     # type: ignore
import scipy.misc as spmisc      # type: ignore
from scipy import LowLevelCallable
import blackscholes


# The following two functions are used to implement Heston's formula with Numba

@numba.njit(complex128(
    float64, float64, float64, float64, float64, float64, complex128, float64))
def _heston_cf(s, v, kappa, theta, sigma, rho, u, t):
    """Characteristic function of the log-price in the Heston model."""
    d = cmath.sqrt((rho*sigma*u*1j - kappa)**2 + sigma**2*(u*1j + u**2))
    g = ((rho*sigma*u*1j - kappa + d) / (rho*sigma*u*1j - kappa - d))
    C = (kappa*theta/sigma**2 * (
        t*(kappa - rho*sigma*u*1j - d) -
        2*cmath.log((1 - g*cmath.exp(-d*t))/(1-g))))
    D = ((kappa - rho*sigma*u*1j - d)/sigma**2 *
         ((1 - cmath.exp(-d*t)) / (1 - g*cmath.exp(-d*t))))
    return cmath.exp(C + D*v + u*math.log(s)*1j)


@numba.cfunc(float64(intc, CPointer(float64)))
def _heston_integrand(n, x):
    """Integrand in Heston's formula."""
    # x = (u, t, k, s, v, kappa, theta, sigma, rho)
    u = x[0]
    t = x[1]
    k = x[2]
    s = x[3]
    v = x[4]
    kappa = x[5]
    theta = x[6]
    sigma = x[7]
    rho = x[8]
    return (cmath.exp(-1j*u*math.log(k))/(1j*u) *
            (_heston_cf(s, v, kappa, theta, sigma, rho, u-1j, t) -
             k*_heston_cf(s, v, kappa, theta, sigma, rho, u, t))).real


@dataclass
class Heston:
    """The Heston model.

    The base asset is stock which under the pricing measure follows the SDEs
        ```
        d(S_t) = r*S_t*dt + sqrt(V_t)*d(W^1_t),
        d(V_t) = kappa*(theta - V_t)*dt + sigma*sqrt(V_t)*d(W^2_t)
        ```
    where `r` is the interest rate, `V_t` is the variance process, `W^1_t` and
    `W^2_t` are standard Brownian motions with correlation coefficient `rho`,
    and `kappa>0, theta>0, sigma>0, -1 < rho < 1` are the model parameters.

    Attributes:
        s: Initial price, i.e. S_0.
        v: Initial variance, i.e. v_0.
        kappa, theta, sigma, rho: Model parameters.
        r: Interest rate.

    Methods:
        call_price: Computes call option price.
        iv: Computes implied volatility produced by the model.
        calibrate: Calibrates parameters of the model.
        simulate_euler: Simulates paths by Euler's scheme.
        simulate_qe: Simulates paths by Andersen's QE scheme.
        simulate_exact: Simulates paths by Broadie-Kaya's exact scheme.
    """
    s: float
    v: float
    kappa: float
    theta: float
    sigma: float
    rho: float
    r: float = 0

    def _call_price_scalar(self, t: float, k: float) -> float:
        """Computes the price of a call option by Heston's semi-closed formula.

        This is an auxiliary function which works with scalar expiration time
        and strike. It is called by `call_price`, which allows vectorization.
        """
        return (0.5*(self.s - math.exp(-self.r*t)*k) +
                1/math.pi * math.exp(-self.r*t) *
                intg.quad(
                    LowLevelCallable(_heston_integrand.ctypes),
                    0, math.inf,
                    args=(t, k, self.s, self.v, self.kappa, self.theta,
                          self.sigma, self.rho))[0])

    def call_price(
        self,
        t: Union[float, NDArray[float_]],
        k: Union[float, NDArray[float_]]
    ) -> Union[float, NDArray[float_]]:
        """Computes the price of a call option by Heston's semi-closed formula.

        Args:
            t: Expiration time (float or ndarray).
            k: Strike (float or ndarray).

        Returns:
            If `t` and `k` are scalars, returns the price of a call option as a
            scalar value. If `t` and/or `k` are arrays, applies NumPy
            broadcasting rules and returns an array of prices.

        Notes:
            Here we use the stable representation of the characteristic
            function, see Albrecher et al. "The little Heston trap" (2007).
        """
        b = np.broadcast(t, k)
        if b.nd:  # Vector arguments were supplied
            return np.fromiter(
                (self._call_price_scalar(t_, k_) for (t_, k_) in b),
                count=b.size, dtype=float_).reshape(b.shape)
        else:
            return self._call_price_scalar(t, k)

    def iv(
        self,
        t: Union[float, NDArray[float_]],
        k: Union[float, NDArray[float_]]
    ) -> Union[float, NDArray[float_]]:
        """Computes the Black-Scholes implied volatility produced by the model.

        This function first computes the price of a call option with expiration
        time `t` and strike `k`, and then inverts the Black-Scholes formula to
        find `sigma`.

        Args:
            t: Expiration time (float or ndarray).
            k: Strike (float or ndarray).

        Returns:
            If `t` and `k` are scalars, returns a scalar value. If `t` and/or
            `k` are arrays, applies NumPy broadcasting rules and returns an
            array. If the implied volatility cannot be computed (i.e. cannot
            solve the Black-Scholes formula for `sigma`), returns NaN in the
            scalar case or puts NaN in the corresponding cell of the array.
        """
        return blackscholes.call_iv(
            self.s, self.r, self.call_price(t, k), t, k)

    @classmethod
    def calibrate(
        cls,
        t: Union[float, NDArray[float_]],
        k: NDArray[float_],
        iv: NDArray[float_],
        s: float,
        r: float = 0,
        min_method: str = "SLSQP",
        return_minimize_result: bool = False
    ):
        """Calibrates the parameters of the Heston model.

        This function finds the parameters `v`, `kappa`, `sigma`, `theta`,
        `rho` of the model which minimize the sum of squares of the differences
        between market and model implied volatilities. Returns an instance of
        the class with the calibrated parameters.

        Args:
            t : Expiration time (scalar or array).
            k: Array of strikes.
            iv: Array of market implied volatilities.
            s: Initial price.
            r: Interest rate.
            min_method: Minimization method to be used, as accepted by
                `scipy.optimize.minimize`. The method must be able to handle
                bounds.
            return_minimize_result: If True, return also the minimization
                result of `scipy.optimize.minimize`.

        Returns:
            If `return_minimize_result` is True, returns a tuple `(cls, res)`,
            where `cls` is an instance of the class with the calibrated
            parameters and `res` in the optimization result returned by
            `scipy.optimize.minimize` (useful for debugging). Otherwise returns
            only `cls`.
        """
        v0 = iv[np.abs(k-s).argmin()]**2  # ATM variance
        res = opt.minimize(
            fun=lambda p: np.linalg.norm(Heston(s, *p, r).iv(t, k) - iv),
            x0=(v0, 1.0, v0, 1.0, -0.5),  # (v, kappa, theta, sigma, rho)
            method=min_method,
            bounds=[(0, math.inf), (0, math.inf), (0, math.inf), (0, math.inf),
                    (-1, 1)])
        ret = cls(s=s, v=res.x[0], kappa=res.x[1], theta=res.x[2],
                  sigma=res.x[3], rho=res.x[4], r=r)
        if return_minimize_result:
            return ret, res
        else:
            return ret

    def simulate_euler(
        self,
        t: float,
        steps: int,
        paths: int,
        return_v: bool = False
    ) -> Union[np.ndarray, tuple[np.ndarray, np.ndarray]]:
        """Simulates paths using Euler's scheme.

        Args:
            t: Time interval.
            steps: Number of simulation points minus 1, i.e. paths are sampled
                at `t_i = i*dt`, where `i = 0, ..., steps`, `dt = t/steps`.
            paths: Number of paths to simulate.
            return_v : If True, returns both price and variance processes.

        Returns:
            If `return_v` is False, returns an array `s` of shape
            `(steps+1, paths)`, where `s[i, j]` is the value of `j`-th path of
            the price process at point `t_i`.

            If `return_v` is True, returns a tuple `(s, v)`, where `s` and `v`
            are arrays of shape `(steps+1, paths)` representing the price and
            variance processes.

        Notes:
            1. Euler's scheme is the fastest but least precise simulation
            method.
            2. Paths of the price process are obtained by simulation of the
            log-price and exponentiation.
            3. Negative values of the variance process are truncated, i.e. the
            coefficients of the SDEs for the log-price and the variance
            processes contain V_t^+.
        """
        dt = t/steps
        Z = st.norm.rvs(size=(2, steps, paths))
        V = np.empty(shape=(steps+1, paths))
        X = np.empty_like(V)
        V[0] = self.v
        X[0] = math.log(self.s)

        for i in range(steps):
            Vplus = np.maximum(V[i], 0)
            V[i+1] = (
                V[i] + self.kappa*(self.theta-Vplus)*dt +
                self.sigma*np.sqrt(Vplus) * Z[0, i]*math.sqrt(dt))
            X[i+1] = (
                X[i] + (self.r-0.5*Vplus)*dt +
                np.sqrt(Vplus)*(
                    self.rho*Z[0, i] +
                    math.sqrt(1-self.rho**2)*Z[1, i])*math.sqrt(dt))
        S = np.exp(X)
        if return_v:
            return S, V
        else:
            return S

    def simulate_qe(
        self,
        t: float,
        steps: int,
        paths: int,
        return_v: bool = False
    ) -> Union[np.ndarray, tuple[np.ndarray, np.ndarray]]:
        """Simulates paths using Andersen's QE scheme.

        See `simulate_euler` for description of arguments and return values.

        Notes:
            This is the most effective simulation method in terms of the
            trade-off between simulation error and speed. This realization does
            not use the martingale correction (see Andersen's paper).
        """
        dt = t/steps
        K0 = -self.rho*self.kappa*self.theta*dt/self.sigma
        K1 = 0.5*(self.kappa*self.rho/self.sigma-0.5)*dt - self.rho/self.sigma
        K2 = 0.5*(self.kappa*self.rho/self.sigma-0.5)*dt + self.rho/self.sigma
        K3 = 0.5*(1-self.rho**2)*dt
        C1 = math.exp(-self.kappa*dt)
        C2 = self.sigma**2*C1*(1-C1)/self.kappa
        C3 = 0.5*self.theta*self.sigma**2*(1-C1)**2/self.kappa
        Z = st.norm.rvs(size=(2, steps, paths))
        U = st.uniform.rvs(size=(steps, paths))
        V = np.empty(shape=(steps+1, paths))
        S = np.empty_like(V)
        V[0] = self.v
        S[0] = self.s

        for i in range(steps):
            m = V[i]*C1 + self.theta*(1-C1)
            s_sq = V[i]*C2 + C3
            psi = s_sq/m**2
            b_sq = np.where(
                psi < 2,
                2/psi - 1 + np.sqrt(np.maximum(4/psi**2 - 2/psi, 0)), 0)
            a = m/(1+b_sq)
            p = np.where(psi > 1, (psi-1)/(psi+1), 0)
            beta = (1-p)/m
            V[i+1] = np.where(psi < 1.5,
                              a*(np.sqrt(b_sq)+Z[0, i])**2,
                              np.where(U[i] < p,
                                       0,
                                       np.log((1-p)/(1-U[i]))/beta))
            S[i+1] = S[i]*np.exp(
                self.r*dt + K0 + K1*V[i] + K2*V[i+1] +
                np.sqrt(K3*(V[i] + V[i+1]))*Z[1, i])
        if return_v:
            return S, V
        else:
            return S

    def _bk_cf(
        self,
        u: float,
        vprev: float,
        vnext: float,
        dt: float
    ) -> complex:
        """Conditional characteristic function of the integrated variance
        process used in Broadie-Kaya's scheme.

        Computes
        `phi(u) = E exp(iu*\\int_t^(t+dt) V_s ds | V_t=vprev, V_(t+dt)=vnext)`
        """
        g = cmath.sqrt(self.kappa**2 - 2*self.sigma**2*u*1j)
        df = 4*self.theta*self.kappa/self.sigma**2
        c1 = math.exp(-self.kappa*dt)
        c2 = cmath.exp(-g*dt)
        return (
            g*cmath.sqrt(c2/c1)*(1 - c1) / (self.kappa*(1 - c2)) *
            cmath.exp((vprev+vnext) / self.sigma**2 *
                      (self.kappa*(1 + c1)/(1 - c1) - g*(1 + c2)/(1 - c2))) *
            spec.iv(0.5*df - 1,
                    cmath.sqrt(vprev*vnext*c2)*4*g/(self.sigma**2*(1 - c2))) /
            spec.iv(0.5*df - 1,
                    math.sqrt(vprev*vnext*c1)*4*self.kappa /
                    (self.sigma**2*(1 - c1))))

    def _bk_prob(
        self, x: float,
        vprev: float,
        vnext: float,
        dt: float,
        truncation_error: float,
        small_tail_stddev: float
    ) -> float:
        """Conditional distribution function of the integrated variance process
        used in Broadie-Kaya's scheme.

        Computes
            `P(\\int_t^{t+dt} V_s ds <= x | V_t=vprev, V_{t+dt}=vnext))`
        by formula (18) from Broadie and Kaya's paper.

        See `simulate_exact` for the description of the parameters
        `truncation_error` and `small_tail_stddev`.
        """
        if (x <= 0):
            return 0
        m = (spmisc.derivative(lambda u: self._bk_cf(u, vprev, vnext, dt), 0,
                               dx=0.001, n=1)/1j).real
        s = math.sqrt(max(
            0, -(spmisc.derivative(lambda u: self._bk_cf(u, vprev, vnext, dt),
                                   0, dx=0.001, n=2)).real))
        h = math.pi/(m + s*small_tail_stddev)
        prob = h*x/math.pi
        j = 1

        # TODO Use a user-specified parameter rather than 1000
        while (j < 1000 and
               cmath.abs(self._bk_cf(h*j, vprev, vnext, dt))/j
               >= math.pi*truncation_error/2):
            prob += (2/math.pi * math.sin(h*j*x)/j *
                     self._bk_cf(h*j, vprev, vnext, dt).real)
            j += 1
        return max(min(prob, 1), 0)

    def simulate_exact(
        self,
        t: float,
        steps: int,
        paths: int,
        truncation_error: float = 1e-5,
        small_tail_stddev: float = 5,
        return_v: bool = False
    ) -> Union[np.ndarray, tuple[np.ndarray, np.ndarray]]:
        """Simulates paths using the exact scheme of Broadie and Kaya.

        See `simulate_euler` for description of arguments and return values, in
        addition to the following two args.

        Args:
            truncation_error: Acceptable error in computation of the
                probability distribution function of the integrated variance.
            small_tail_stddev : Number of standard deviations from the mean to
                assume that the tail of the probability distribution function
                of the integrated variance is smaller than truncation_error.

        Notes:
            1. This is the slowest method, but it exactly reproduces the
            probability distributions of the price and variance processes
            (modulo truncation errors in inversion of the characteristic
            function). Hence there is no need to choose large `steps` if a
            whole path is not required, e.g. for payoffs depending only on the
            final price (in this case, `steps=1` will do the job).
            2. Parameter `truncation_error` corresponds to `epsilon` in Broadie
            and Kaya's paper, see there formula (15).
            3. The value of `u_epsilon` from the paper (see (17)) is selected
            as `m + small_tail_stddev*sigma`, where `m` and `sigma` are the
            mean and standard deviation of `\\int_{t_i}^{t_{i+1}} V_s ds`,
            which are estimated by numerically differentiating the
            characteristic function.
        """

        dt = t/steps
        df = 4*self.theta*self.kappa/self.sigma**2
        nc = (4*self.kappa*math.exp(-self.kappa*dt) /
              (self.sigma**2*(1-math.exp(-self.kappa*dt))))
        Z = st.norm.rvs(size=(steps, paths))
        # Multiplication by 0.9999 is done to avoid values too close to 1,
        # which will cause problems with inversion of the distribution function
        U = st.uniform.rvs(size=(steps, paths))*0.9999
        S = np.empty(shape=(steps+1, paths))
        V = np.empty_like(S)
        int_v = np.empty(paths)  # stores integrated variance at current step
        S[0] = self.s
        V[0] = self.v

        for i in range(steps):
            V[i+1] = (
                self.sigma**2*(1-math.exp(-self.kappa*dt))/(4*self.kappa) *
                st.ncx2(df=df, nc=nc*V[i]).rvs(size=paths))
            for j in range(paths):
                # Trying to find F^{-1}(U) where F is the conditional
                # distribution of the integrated variance.
                # For safety, truncate the support of F at max_int_v
                max_int_v = (V[i, j] + V[i+1, j])*dt*10
                if self._bk_prob(
                        max_int_v, V[i, j], V[i+1, j], dt,
                        truncation_error, small_tail_stddev) <= U[i, j]:
                    int_v[j] = max_int_v
                else:
                    int_v[j] = opt.brentq(
                        lambda x: self._bk_prob(
                            x, V[i, j], V[i+1, j], dt, truncation_error,
                            small_tail_stddev) - U[i, j],
                        a=0, b=max_int_v)
            # The two stochastic integrals
            int_w1 = (V[i+1]-V[i] - self.kappa*self.theta*dt +
                      self.kappa*int_v) / self.sigma
            int_w2 = Z[i]*math.sqrt(int_v)

            S[i+1] = S[i]*math.exp(
                self.r*dt - 0.5*int_v + self.rho*int_w1 +
                math.sqrt(1-self.rho**2)*int_w2)

        if return_v:
            return S, V
        else:
            return S
