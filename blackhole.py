"""Two dimensional hydrostatic tank with gravitational force due to a primordial black hole. Modified from hydrostatic_tank by Brady Metherall"""

import os.path

import numpy as np

# PyZoltan imports
from pyzoltan.core.carray import LongArray

# PySPH imports
from pysph.base.utils import get_particle_array_wcsph as gpa
from pysph.base.kernels import Gaussian, WendlandQuintic, CubicSpline, QuinticSpline
from pysph.solver.solver import Solver
from pysph.solver.application import Application
from pysph.sph.integrator import PECIntegrator
from pysph.sph.integrator_step import WCSPHStep

# the eqations
from pysph.sph.equation import Group
from pysph.sph.BlackHoleEquation import BlackHole2D

# Equations for REF1
from pysph.sph.wc.transport_velocity import VolumeFromMassDensity,\
    ContinuityEquation,\
    MomentumEquationPressureGradient, \
    MomentumEquationArtificialViscosity,\
    SolidWallPressureBC

# Monaghan type repulsive boundary forces used in REF(2)
from pysph.sph.boundary_equations import MonaghanBoundaryForce,\
    MonaghanKajtarBoundaryForce

# Equations for the standard WCSPH formulation and dynamic boundary
# conditions defined in REF3
from pysph.sph.wc.basic import TaitEOS, TaitEOSHGCorrection, MomentumEquation
from pysph.sph.basic_equations import XSPHCorrection, \
    MonaghanArtificialViscosity

# domain and reference values
Lx = 60.0; Ly = 30.0; H = 0.8*Ly
gy = -1.0
Vmax = np.sqrt(abs(gy) * H)
c0 = 10 * Vmax; rho0 = 1.0
p0 = c0*c0*rho0
gamma = 1.0

soft = 0.05
t_hit = 10.0
Mass = 1.0

# Reynolds number and kinematic viscosity
Re = 0; nu = 0.01#Vmax * Ly/Re

# Numerical setup
nx = 250; dx = Lx/nx
ghost_extent = 5.5 * dx
hdx = 1.2

# adaptive time steps
h0 = hdx * dx
dt_cfl = 0.25 * h0/( c0 + Vmax )
dt_viscous = 0.125 * h0**2/nu
dt_force = 0.25 * np.sqrt(h0/abs(gy))

tdamp = 1.0
tf = t_hit + H
dt = 0.75 * min(dt_cfl, dt_viscous, dt_force)
output_at_times = np.arange(0.25, 2.1, 0.25)


def damping_factor(t, tdamp):
    if t < tdamp:
        return 0.5 * ( np.sin((-0.5 + t/tdamp)*np.pi)+ 1.0 )
    else:
        return 1.0


class HydrostaticTank(Application):
    def add_user_options(self, group):
        group.add_argument(
            '--bc-type', action='store', type=int,
            dest='bc_type', default=1,
            help="Specify the implementation type one of (1, 2, 3)"
        )

    def create_particles(self):
        # create all the particles
        _x = np.arange( -0.5*Lx - ghost_extent, 0.5*Lx + ghost_extent, dx )
        _y = np.arange( -H-ghost_extent, -H+Ly, dx )
        x, y = np.meshgrid(_x, _y); x = x.ravel(); y = y.ravel()

        # sort out the fluid and the solid
        indices = []
        for i in range(x.size):
            if ( (x[i] > -0.5*Lx) and (x[i] < 0.5*Lx) ):
                if ( (y[i] > -H) and (y[i] < 0) ):
                    indices.append(i)

        # create the arrays
        solid = gpa(name='solid', x=x, y=y)

        # remove the fluid particles from the solid
        fluid = solid.extract_particles(indices); fluid.set_name('fluid')
        solid.remove_particles(indices)

        # remove the lid to generate an open tank
        indices = []
        for i in range(solid.get_number_of_particles()):
            if solid.y[i] > 0:
                if (-0.5*Lx < solid.x[i] < 0.5*Lx):
                    indices.append(i)
        solid.remove_particles(indices)

        print("Hydrostatic tank with primordial black hole :: nfluid = %d, nsolid=%d, dt = %g"%(
            fluid.get_number_of_particles(),
            solid.get_number_of_particles(), dt))

        ###### ADD PARTICLE PROPS FOR MULTI-PHASE SPH ######

        # particle volume
        fluid.add_property('V')
        solid.add_property('V' )

        # kernel sum term for boundary particles
        solid.add_property('wij')

        # advection velocities and accelerations
        for name in ('auhat', 'avhat', 'awhat'):
            fluid.add_property(name)

        ##### INITIALIZE PARTICLE PROPS #####
        fluid.rho[:] = rho0
        solid.rho[:] = rho0

        fluid.rho0[:] = rho0
        solid.rho0[:] = rho0

        # mass is set to get the reference density of rho0
        volume = dx * dx

        # volume is set as dx^2
        fluid.V[:] = 1./volume
        solid.V[:] = 1./volume

        fluid.m[:] = volume * rho0
        solid.m[:] = volume * rho0

        # smoothing lengths
        fluid.h[:] = hdx * dx
        solid.h[:] = hdx * dx

        # return the particle list
        return [fluid, solid]

    def create_solver(self):
        # Create the kernel
        #kernel = Gaussian(dim=2)
        kernel = QuinticSpline(dim=2)

        integrator = PECIntegrator(fluid=WCSPHStep())

        # Create a solver.
        solver = Solver(kernel=kernel, dim=2, integrator=integrator,
                        tf=tf, dt=dt, output_at_times=output_at_times)
        return solver

    def create_equations(self):
        # Formulation for REF1
        equations1 = [
            # For the multi-phase formulation, we require an estimate of the
            # particle volume. This can be either defined from the particle
            # number density or simply as the ratio of mass to density.
            Group(equations=[
                    VolumeFromMassDensity(dest='fluid', sources=None)
                    ], ),

            # Equation of state is typically the Tait EOS with a suitable
            # exponent gamma
            Group(equations=[
                    TaitEOS(dest='fluid', sources=None, rho0=rho0, c0=c0, gamma=gamma),
                    ], ),

            # The boundary conditions are imposed by extrapolating the fluid
            # pressure, taking into considering the bounday acceleration
            Group(equations=[
                    SolidWallPressureBC(dest='solid', sources=['fluid'], b=1.0, gy=gy,
                                        rho0=rho0, p0=p0),
                    ], ),

            # Main acceleration block
            Group(equations=[

                    # Continuity equation
                    ContinuityEquation(dest='fluid', sources=['fluid','solid']),

                    # Pressure gradient with acceleration damping.
                    MomentumEquationPressureGradient(
                        dest='fluid', sources=['fluid', 'solid'], pb=0.0, gy=gy,
                        tdamp=tdamp),

                    # artificial viscosity for stability
                    MomentumEquationArtificialViscosity(
                        dest='fluid', sources=['fluid', 'solid'], alpha=0.24, c0=c0),

                    # Position step with XSPH
                    XSPHCorrection(dest='fluid', sources=['fluid'], eps=0.0),

                    # Add a PBH
                    BlackHole2D(dest='fluid', sources=['solid'], soft=soft, t_hit=t_hit, M=Mass)

                    ]),
            ]

        # Formulation for REF2. Note that for this formulation to work, the
        # boundary particles need to have a spacing different from the fluid
        # particles (usually determined by a factor beta). In the current
        # implementation, the value is taken as 1.0 which will mostly be
        # ineffective.
        equations2 = [
            # For the multi-phase formulation, we require an estimate of the
            # particle volume. This can be either defined from the particle
            # number density or simply as the ratio of mass to density.
            Group(equations=[
                    VolumeFromMassDensity(dest='fluid', sources=None)
                    ], ),

            # Equation of state is typically the Tait EOS with a suitable
            # exponent gamma
            Group(equations=[
                    TaitEOS(dest='fluid', sources=None, rho0=rho0, c0=c0, gamma=gamma),
                    ], ),

            # Main acceleration block
            Group(equations=[

                    # The boundary conditions are imposed as a force or
                    # accelerations on the fluid particles. Note that the
                    # no-penetration condition is to be satisfied with this
                    # equation. The subsequent equations therefore do not have
                    # solid as the source. Note the difference between the
                    # ghost-fluid formulations. K should be 0.01*co**2
                    # according to REF2. We take it much smaller here on
                    # account of the multiple layers of boundary particles
                    MonaghanKajtarBoundaryForce(dest='fluid', sources=['solid'],
                                                K=0.02, beta=1.0, h=hdx*dx),

                    # Continuity equation
                    ContinuityEquation(dest='fluid', sources=['fluid',]),

                    # Pressure gradient with acceleration damping.
                    MomentumEquationPressureGradient(
                        dest='fluid', sources=['fluid'], pb=0.0, gy=gy,
                        tdamp=tdamp),

                    # artificial viscosity for stability
                    MomentumEquationArtificialViscosity(
                        dest='fluid', sources=['fluid'], alpha=0.25, c0=c0),

                    # Position step with XSPH
                    XSPHCorrection(dest='fluid', sources=['fluid'], eps=0.0),

                    # Add a PBH
                    BlackHole2D(dest='fluid', sources=['solid'], soft=soft, t_hit=t_hit, M=Mass)

                    ]),
            ]

        # Formulation for REF3
        equations3 = [
            # For the multi-phase formulation, we require an estimate of the
            # particle volume. This can be either defined from the particle
            # number density or simply as the ratio of mass to density.
            Group(equations=[
                    VolumeFromMassDensity(dest='fluid', sources=None)
                    ], ),

            # Equation of state is typically the Tait EOS with a suitable
            # exponent gamma. The solid phase is treated just as a fluid and
            # the pressure and density operations is updated for this as well.
            Group(equations=[
                    TaitEOS(dest='fluid', sources=None, rho0=rho0, c0=c0, gamma=gamma),
                    TaitEOS(dest='solid', sources=None, rho0=rho0, c0=c0, gamma=gamma),
                    ], ),

            # Main acceleration block. The boundary conditions are imposed by
            # peforming the continuity equation and gradient of pressure
            # calculation on the solid phase, taking contributions from the
            # fluid phase
            Group(equations=[

                    # Continuity equation
                    ContinuityEquation(dest='fluid', sources=['fluid','solid']),
                    ContinuityEquation(dest='solid', sources=['fluid']),

                    # Pressure gradient with acceleration damping.
                    MomentumEquationPressureGradient(
                        dest='fluid', sources=['fluid', 'solid'], pb=0.0, gy=gy,
                        tdamp=tdamp),

                    # artificial viscosity for stability
                    MomentumEquationArtificialViscosity(
                        dest='fluid', sources=['fluid', 'solid'], alpha=0.25, c0=c0),

                    # Position step with XSPH
                    XSPHCorrection(dest='fluid', sources=['fluid'], eps=0.5),

                    # Add a PBH
                    BlackHole2D(dest='fluid', sources=['solid'], soft=soft, t_hit=t_hit, M=Mass)

                    ]),
            ]

        if self.options.bc_type == 1:
            return equations1
        elif self.options.bc_type == 2:
            return equations2
        elif self.options.bc_type == 3:
            return equations3

    def post_process(self, info_fname):
        self.read_info(info_fname)
        if len(self.output_files) == 0:
            return

        from pysph.tools.interpolator import Interpolator
        from pysph.solver.utils import iter_output
        files = self.output_files
        y = np.linspace(0, 0.9, 20)
        x = np.ones_like(y)
        interp = None
        t, p, p_ex = [], [], []
        for sd, arrays in iter_output(files):
            fluid, solid = arrays['fluid'], arrays['solid']
            if interp is None:
                interp = Interpolator([fluid, solid], x=x, y=y)
            else:
                interp.update_particle_arrays([fluid, solid])
            t.append(sd['t'])
            p.append(interp.interpolate('p'))
            g = 1.0*damping_factor(t[-1], tdamp)
            p_ex.append(abs(rho0*H*g))

        t, p, p_ex = list(map(np.asarray, (t, p, p_ex)))
        res = os.path.join(self.output_dir, 'results.npz')
        np.savez(res, t=t, p=p, p_ex=p_ex, y=y)

        import matplotlib
        matplotlib.use('Agg')

        pmax = abs(0.9*rho0*gy)

        from matplotlib import pyplot as plt
        plt.plot(t, p[:,0]/pmax, 'o-')
        plt.xlabel(r'$t$'); plt.ylabel(r'$p$')
        fig = os.path.join(self.output_dir, 'p_bottom.png')
        plt.savefig(fig, dpi=300)

        plt.clf()
        output_at = np.arange(0.25, 2.1, 0.25)
        count = 0
        for i in range(len(t)):
            if abs(t[i] - output_at[count]) < 1e-8:
                plt.plot(y, p[i]/pmax, 'o', label='t=%.2f'%t[i])
                plt.plot(y, p_ex[i]*(H-y)/(H*pmax), 'k-')
                count += 1
        plt.xlabel('$y$'); plt.ylabel('$p$')
        plt.legend()
        fig = os.path.join(self.output_dir, 'p_vs_y.png')
        plt.savefig(fig, dpi=300)


if __name__ == '__main__':
    app = HydrostaticTank()
    app.run()
    app.post_process(app.info_filename)
